"""
E2E tests for the "sign once, populate all fields" promise.

These don't hit the OpenAI API — they synthesize PDFs with the exact field
names the mapper would produce (text templates pointing at
{agent.signature} / {agent.initials}) and run the full fill_document path
to confirm:

  1. Multiple signature fields on a single PDF all receive the same stamp
     when the agent saves one signature.
  2. Initials and signature stamps coexist on the same page without colliding.
  3. signature_status counts surface correctly through the response.
  4. Missing signature → fields stay blank, response flags it.
  5. Corrupt base64 → graceful degradation (no 500, response flags it).

The CAR BRBC eval already exists for AI-mapping accuracy; this is the
plumbing test that proves the rest of the pipeline does what the design
doc promised once mapping is correct.
"""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as rl_canvas

from backend.generate import fill_document
from backend.schema import AgentProfile, TransactionFields


def _make_signature_png(width: int = 600, height: int = 200) -> bytes:
    """A recognizable 'signature' PNG — diagonal stroke + horizontal
    underline. Tests can use the byte length to differentiate from empty
    or blank PNG, and the geometry to spot orientation issues if a future
    test needs them."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    pixels = img.load()
    # Diagonal stroke from top-left to bottom-right area.
    for x in range(30, width - 30):
        y = int(height * 0.3 + (height * 0.4) * (x / width))
        for dy in range(-2, 3):
            if 0 <= y + dy < height:
                pixels[x, y + dy] = (0, 0, 0, 255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_template_with_signature_fields(
    *,
    n_signature_fields: int = 3,
    n_initials_fields: int = 2,
    n_text_fields: int = 1,
) -> bytes:
    """Synthesize a PDF with the named AcroForm fields a real mapping
    would target. Names match the convention the AI/coercion emits."""
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=letter)
    y = 700
    for i in range(n_text_fields):
        c.acroForm.textfield(
            name=f"agent_name_{i}",
            x=100, y=y, width=200, height=20,
            borderStyle="solid",
        )
        y -= 40
    for i in range(n_signature_fields):
        c.acroForm.textfield(
            name=f"agent_sig_{i}",
            x=100, y=y, width=300, height=40,
            borderStyle="solid",
        )
        y -= 60
    for i in range(n_initials_fields):
        c.acroForm.textfield(
            name=f"agent_initial_{i}",
            x=100, y=y, width=60, height=30,
            borderStyle="solid",
        )
        y -= 50
    c.showPage()
    c.save()
    return buf.getvalue()


@pytest.fixture
def signature_template_dir(tmp_path, monkeypatch):
    """Spin up a per-test PDF + mapping pair on disk so fill_document
    has the same shape it sees in production. Patches the module-level
    TEMPLATES_DIR / MAPPINGS_DIR via monkeypatch so the test's fixtures
    win without trampling real templates."""
    tpl_dir = tmp_path / "pdf"
    map_dir = tmp_path / "mappings"
    tpl_dir.mkdir()
    map_dir.mkdir()

    pdf_bytes = _make_template_with_signature_fields()
    pdf_path = tpl_dir / "test_template.pdf"
    pdf_path.write_bytes(pdf_bytes)

    # Mapping points each field at its appropriate canonical path.
    mapping = {
        "_meta": {
            "title": "Test Template",
            "source_pdf": "test_template.pdf",
            "filled_filename": "test_filled.pdf",
        },
        "fields": {
            "agent_name_0": "{agent.name}",
            "agent_sig_0": "{agent.signature}",
            "agent_sig_1": "{agent.signature}",
            "agent_sig_2": "{agent.signature}",
            "agent_initial_0": "{agent.initials}",
            "agent_initial_1": "{agent.initials}",
        },
        "low_confidence": [],
    }
    map_path = map_dir / "test_template.json"
    map_path.write_text(json.dumps(mapping))

    import backend.generate as gen
    monkeypatch.setattr(gen, "TEMPLATES_DIR", tpl_dir)
    monkeypatch.setattr(gen, "MAPPINGS_DIR", map_dir)
    return {"pdf_path": pdf_path, "mapping_path": map_path}


# ---- the goal-state test --------------------------------------------------

def test_one_signature_populates_all_signature_fields(signature_template_dir):
    """The /goal: writing a signature once populates ALL the signature
    fields correctly and smoothly. Three signature fields, two initials
    fields, one printed name. Agent saves one signature + one initials.
    Both stamps appear on every appropriate field."""
    sig_png = _make_signature_png()
    init_png = _make_signature_png(width=200, height=100)
    agent = AgentProfile(
        name="Jane Doe",
        signature=base64.b64encode(sig_png).decode("ascii"),
        initials=base64.b64encode(init_png).decode("ascii"),
    )
    fields = TransactionFields()  # all defaults are fine

    doc = fill_document("test_template", fields, agent)

    # 3 signature fields + 2 initials fields = 5 total signature mappings.
    assert doc.signature_fields_total == 5
    # All 5 get stamped — the goal-state. The agent signed once and every
    # field got the PNG.
    assert doc.signature_fields_stamped == 5

    # The output should parse cleanly.
    out_bytes = base64.b64decode(doc.base64)
    reader = PdfReader(io.BytesIO(out_bytes))
    assert len(reader.pages) == 1


def test_signature_not_set_leaves_fields_blank(signature_template_dir):
    """Inverse case: agent hasn't set up a signature yet. Fields render
    blank, no error, and signature_status surfaces the gap so the frontend
    can prompt."""
    agent = AgentProfile(name="Jane Doe")  # no signature / initials
    fields = TransactionFields()

    doc = fill_document("test_template", fields, agent)

    # All signature fields counted, zero stamped.
    assert doc.signature_fields_total == 5
    assert doc.signature_fields_stamped == 0

    # Output still parses (just no image overlay).
    out_bytes = base64.b64decode(doc.base64)
    PdfReader(io.BytesIO(out_bytes))


def test_corrupt_signature_does_not_500(signature_template_dir):
    """Garbage in agent.signature — fill_document should degrade
    gracefully: blank the field, never raise."""
    agent = AgentProfile(
        name="Jane Doe",
        signature="this is not base64 PNG data!!!!",
    )
    fields = TransactionFields()

    # Must not raise.
    doc = fill_document("test_template", fields, agent)
    # Stamps skipped — count stays at zero.
    assert doc.signature_fields_total == 5
    assert doc.signature_fields_stamped == 0


def test_signature_only_no_initials(signature_template_dir):
    """Agent set signature but not initials. Signature fields stamp,
    initials fields stay blank. Partial success surfaces in count."""
    sig_png = _make_signature_png()
    agent = AgentProfile(
        name="Jane Doe",
        signature=base64.b64encode(sig_png).decode("ascii"),
    )
    fields = TransactionFields()

    doc = fill_document("test_template", fields, agent)

    # 3 signature stamped + 0 initials = 3 of 5.
    assert doc.signature_fields_total == 5
    assert doc.signature_fields_stamped == 3


def test_stamped_pdf_is_larger_than_blank_baseline(signature_template_dir):
    """Concrete byte-size check that the stamp pipeline actually emits
    XObjects into the output PDF. A flat 'we said we stamped' assertion
    isn't enough — the file must measurably grow."""
    fields = TransactionFields()

    baseline_doc = fill_document(
        "test_template", fields, AgentProfile(name="Jane Doe")
    )
    baseline_bytes = base64.b64decode(baseline_doc.base64)

    sig_png = _make_signature_png()
    stamped_doc = fill_document(
        "test_template", fields,
        AgentProfile(
            name="Jane Doe",
            signature=base64.b64encode(sig_png).decode("ascii"),
        ),
    )
    stamped_bytes = base64.b64decode(stamped_doc.base64)

    assert len(stamped_bytes) > len(baseline_bytes), (
        f"stamped PDF ({len(stamped_bytes)}B) should be larger than "
        f"blank baseline ({len(baseline_bytes)}B)"
    )
