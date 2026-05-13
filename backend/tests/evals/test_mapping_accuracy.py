"""
Mapping accuracy evaluation harness.

Marked `evals` so it doesn't run by default in CI. To run:

    pytest backend/tests/evals/ -m evals --tb=short

This harness exercises the REAL OpenAI API against ground-truth fixtures
and reports per-template accuracy across MULTIPLE trials. Without N-trial
runs, a single-shot eval reports a best-of-one number — stochastic API
variance can mask a real regression below the floor. We run NUM_TRIALS
times per template, report min/mean/max, and gate on MIN.

Why MIN-gate: users see the trial they hit, not the average. If a template
hits 100/100/95/82/97% across 5 trials, the user who hit 82% has a worse
experience than the headline mean of 95% suggests. The 82% is what we
defend against regressions.

Ground truth source: the hand-crafted expected JSONs in fixtures/, generated
by visual inspection of each template. Those are the gold standard. Input
fixtures (the *_fields.json files) are rebuilt via /tmp/rebuild_eval_fixtures.py
when the descriptor shape changes (e.g. new spatial signals).

Per-template MIN targets (the floor — anything below means regression):
  Lease Invoice:    >= 85% (small, well-labeled — 1 wrong = 14 points)
  Lease Abstract:   >= 80%
  Multi-Board:      >= 60% (389 fields, hardest case; stretch goal 80%)
  CAR BRBC:         >= 50% (flattened PDF, 165 synth fields; baseline)

The accuracy bar drops on Multi-Board + CAR because:
- Hundreds of fields stress the AI's context window
- Many fields have no neighbor text (rely on visual crops)
- Synthesized fields have meaningless f_NNN_NNN names
- Hand-curation of expected.json is itself judgment-dependent
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

# The shared conftest sets OPENAI_API_KEY=sk-test as a placeholder so unit
# tests with mocked OpenAI don't error out at import time. Evals need the
# REAL key, so override the placeholder from .env before the OpenAI client
# is constructed. override=True is the load matters — without it the
# setdefault('sk-test') from conftest wins and every eval hits OpenAI with
# a fake key, returning auth errors that the test infrastructure can't
# distinguish from a genuine mapping regression.
load_dotenv(override=True)
if not os.environ.get("OPENAI_API_KEY") or os.environ["OPENAI_API_KEY"] == "sk-test":
    pytest.skip(
        "Evals require a real OPENAI_API_KEY in .env (got placeholder). "
        "Set the key and re-run with `pytest -m evals`.",
        allow_module_level=True,
    )

from backend.pdf_render import collect_field_crops
from backend.templates import propose_mapping_two_pass, validate_pdf

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# Number of trials per template. Higher = tighter MIN estimate, longer
# eval run. 3 is the smallest N where a single bad trial is visibly
# below the median; 5 is more stable but doubles cost. Set via env var
# for ad-hoc deeper runs ($ EVAL_TRIALS=5 pytest ...).
NUM_TRIALS = int(os.environ.get("EVAL_TRIALS", "3"))


def _score(proposal, expected: dict[str, str | None]) -> tuple[int, int, int, int]:
    """Return (correct, wrong, missing, total) by comparing proposal to expected."""
    correct = 0
    wrong = 0
    missing = 0
    by_field = {p.pdf_field: p for p in proposal.fields}
    for pdf_field, expected_path in expected.items():
        got = by_field.get(pdf_field)
        if got is None:
            if expected_path is None:
                correct += 1
            else:
                missing += 1
            continue
        got_path = got.canonical_path
        if expected_path == got_path:
            correct += 1
        elif expected_path is None:
            wrong += 1
        elif got_path is None:
            missing += 1
        else:
            wrong += 1
    return correct, wrong, missing, len(expected)


@pytest.mark.evals
@pytest.mark.parametrize("template_name,pdf_filename,min_target", [
    (
        "lease_invoice",
        "2025 Compass Chicagoland Lease Invoice Landlords and Tenant Use copy.pdf",
        0.85,
    ),
    (
        "lease_abstract",
        "Lease Abstract Form.pdf",
        0.80,
    ),
    (
        "multiboard",
        "Multi-Board-8.0 (1).pdf",
        0.60,
    ),
    (
        "car_brbc",
        # CAR uses the flattened-PDF synth path. Source PDF lives in the
        # test fixtures dir (the canonical test rig copy); the template
        # pipeline persists synth output to templates/pdf/ on real upload.
        "__synth__",
        0.50,
    ),
])
async def test_mapping_accuracy(template_name, pdf_filename, min_target):
    """Run real AI mapping NUM_TRIALS times against ground truth. Reports
    per-trial accuracy + min/mean/max. Fails when MIN accuracy drops below
    min_target — the floor matters more than the headline mean because
    users experience the trial they hit, not the average."""
    fields_fixture = FIXTURES_DIR / f"{template_name}_fields.json"
    expected_fixture = FIXTURES_DIR / f"{template_name}_expected.json"

    # Source PDF lookup. Most templates live in templates/pdf/ (the seed
    # set used during early development); the CAR BRBC source lives under
    # backend/tests/fixtures/ where it was captured for the synth pipeline
    # tests.
    if pdf_filename == "__synth__":
        fixtures_pdf = Path(__file__).resolve().parent.parent / "fixtures" / "car_brbc_flattened.pdf"
    else:
        fixtures_pdf = Path(__file__).resolve().parent.parent.parent.parent / "templates" / "pdf" / pdf_filename

    if not all(p.exists() for p in (fixtures_pdf, fields_fixture, expected_fixture)):
        pytest.skip(f"Missing fixtures for {template_name}")

    pdf_bytes = fixtures_pdf.read_bytes()
    reader, _persist_bytes = validate_pdf(pdf_bytes)
    field_descs = json.loads(fields_fixture.read_text())
    expected: dict[str, str | None] = json.loads(expected_fixture.read_text())

    # Render visual crops once — they're deterministic, the variance lives
    # in the AI calls, not the input prep.
    crops = collect_field_crops(reader, field_descs)

    accuracies: list[float] = []
    per_trial: list[tuple[int, int, int]] = []  # (correct, wrong, missing)
    for trial in range(NUM_TRIALS):
        proposal = await propose_mapping_two_pass(field_descs, crops=crops)
        correct, wrong, missing, total = _score(proposal, expected)
        acc = correct / total if total else 0.0
        accuracies.append(acc)
        per_trial.append((correct, wrong, missing))
        print(f"\n{template_name} trial {trial+1}/{NUM_TRIALS}: "
              f"{correct}/{total} = {acc:.0%} (wrong={wrong} missing={missing})")

    min_acc = min(accuracies)
    max_acc = max(accuracies)
    mean_acc = sum(accuracies) / len(accuracies)
    spread = max_acc - min_acc

    print(f"\n{template_name} across {NUM_TRIALS} trials:")
    print(f"  min:    {min_acc:.0%}")
    print(f"  mean:   {mean_acc:.0%}")
    print(f"  max:    {max_acc:.0%}")
    print(f"  spread: {spread:.0%}")

    assert min_acc >= min_target, (
        f"{template_name} MIN accuracy {min_acc:.0%} below target {min_target:.0%} "
        f"(mean {mean_acc:.0%}, max {max_acc:.0%}, spread {spread:.0%}). "
        f"Per-trial: {per_trial}"
    )
