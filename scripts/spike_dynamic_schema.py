"""
SPIKE: dynamic Pydantic + OpenAI Responses API.

Question we need answered before committing the Pillar 2 architecture:
can we build a Pydantic model at runtime that extends TransactionFields with
per-template extra_fields, pass it to client.responses.parse(text_format=...),
and get back a clean parse?

Tests, in order:
  1. Baseline: existing TransactionFields call still works.
  2. Empty extras: dynamic model with no actual template fields.
  3. One template: pet addendum with 3 nullable text fields.
  4. Two templates: pet addendum + pool disclosure simultaneously.
  5. Strict-mode schema audit: dump the JSON schema and check OpenAI constraints
     (all properties required, no additionalProperties=true, depth <=5, <=100 props).
  6. Cache behavior: 5 calls same shape vs 5 calls different shapes,
     compare cached_tokens in usage.

Run: .venv/bin/python scripts/spike_dynamic_schema.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Make backend.* importable when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from openai import OpenAI
from pydantic import BaseModel, Field, create_model

from backend.schema import TransactionFields

MODEL = "gpt-5"

LEASE_NOTES = """\
221 W Hubbard Unit 803 Chicago, IL 60654
$3182 monthly rent
5/4/26 - 7/3/27
$3182 co-op
hubbard221leasing@draperandkramer.com — for invoice
Tenant: John Doe, john@example.com 312-555-0100
Pet: golden retriever named Lucy, $300 pet deposit
Pool: in-ground heated, season runs Apr-Oct
"""

LEASE_NOTES_NO_EXTRAS = """\
221 W Hubbard Unit 803 Chicago, IL 60654
$3182 monthly rent
5/4/26 - 7/3/27
Tenant: John Doe
"""

SYSTEM_PROMPT = """\
You extract structured transaction data from a real estate buyer-agent's notes.
Return null for any field the notes don't mention.
"""


def _print_section(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


def _make_pet_extras_model():
    """Pet addendum extras: 3 nullable text fields."""
    return create_model(
        "PetAddendumExtras",
        pet_name=(str | None, Field(None, description="Pet's name")),
        pet_breed=(str | None, Field(None, description="Pet's breed or species")),
        pet_deposit=(str | None, Field(None, description="Pet deposit amount, as written")),
    )


def _make_pool_extras_model():
    return create_model(
        "PoolDisclosureExtras",
        pool_type=(str | None, Field(None, description="In-ground / above-ground / saltwater / heated")),
        pool_season=(str | None, Field(None, description="Months pool is open")),
    )


def _make_dynamic_model(template_extras: dict[str, type[BaseModel]]):
    """Build TransactionFieldsExtended with template_extras as a nested object.

    Shape we want in the JSON the LLM produces:
      {
        ...all TransactionFields fields...,
        "template_extras": {
          "pet_addendum": {"pet_name": "Lucy", ...},
          "pool_disclosure": {...}
        }
      }
    """
    if not template_extras:
        # Even with zero templates we still want a key, just empty.
        ExtrasContainer = create_model("EmptyExtrasContainer", __base__=BaseModel)
    else:
        # Build a container model whose fields are each template's extras model.
        container_fields = {
            tpl_id: (model | None, Field(None, description=f"Extras for template {tpl_id!r}"))
            for tpl_id, model in template_extras.items()
        }
        ExtrasContainer = create_model("ExtrasContainer", **container_fields)

    return create_model(
        "TransactionFieldsExtended",
        __base__=TransactionFields,
        template_extras=(ExtrasContainer | None, Field(None, description="Per-template additional fields")),
    )


def _audit_schema(model: type[BaseModel]) -> dict:
    """Inspect the JSON schema OpenAI will see. Looking for strict-mode pitfalls."""
    schema = model.model_json_schema()
    issues: list[str] = []

    def walk(node, depth=0, path="$"):
        if depth > 5:
            issues.append(f"depth>5 at {path}")
        if not isinstance(node, dict):
            return
        if node.get("additionalProperties") is True:
            issues.append(f"additionalProperties=true at {path}")
        # Count props at this level
        if "properties" in node:
            for k, v in node["properties"].items():
                walk(v, depth + 1, f"{path}.{k}")
        for k in ("items", "anyOf", "oneOf", "allOf"):
            v = node.get(k)
            if isinstance(v, list):
                for i, sub in enumerate(v):
                    walk(sub, depth + 1, f"{path}.{k}[{i}]")
            elif isinstance(v, dict):
                walk(v, depth + 1, f"{path}.{k}")
        # $defs/components
        for k in ("$defs", "definitions"):
            v = node.get(k)
            if isinstance(v, dict):
                for name, sub in v.items():
                    walk(sub, depth + 1, f"{path}.{k}.{name}")

    walk(schema)

    # Total property count across the whole schema
    def count_props(node) -> int:
        if not isinstance(node, dict):
            return 0
        n = len(node.get("properties", {}))
        for v in (node.get("$defs") or {}).values():
            n += count_props(v)
        return n

    total_props = count_props(schema)
    if total_props > 100:
        issues.append(f"property count {total_props} > 100 (OpenAI strict-mode cap)")

    return {"issues": issues, "total_props": total_props, "schema_size_chars": len(json.dumps(schema))}


def _call(client: OpenAI, model_class: type[BaseModel], notes: str) -> tuple[BaseModel | None, dict]:
    """Single Responses API call. Returns (parsed, usage_dict)."""
    t0 = time.time()
    response = client.responses.parse(
        model=MODEL,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [{"type": "input_text", "text": notes}]},
        ],
        text_format=model_class,
    )
    elapsed = time.time() - t0
    usage = response.usage
    return response.output_parsed, {
        "elapsed_s": round(elapsed, 2),
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "cached_tokens": getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0),
    }


def main() -> None:
    client = OpenAI()
    results: dict[str, dict] = {}

    # ----- 1. Baseline -----
    _print_section("Test 1: Baseline (existing TransactionFields)")
    parsed, usage = _call(client, TransactionFields, LEASE_NOTES_NO_EXTRAS)
    print(f"  ✓ parsed: tx_type={parsed.transaction_type}, addr={parsed.property.address!r}")
    print(f"  usage: {usage}")
    results["baseline"] = {"ok": True, "usage": usage}

    # ----- 2. Empty extras -----
    _print_section("Test 2: Dynamic model with empty extras container")
    EmptyModel = _make_dynamic_model({})
    audit = _audit_schema(EmptyModel)
    print(f"  schema audit: {audit}")
    if audit["issues"]:
        print(f"  ⚠ schema issues: {audit['issues']}")
    parsed, usage = _call(client, EmptyModel, LEASE_NOTES_NO_EXTRAS)
    print(f"  ✓ parsed core: addr={parsed.property.address!r}, extras={parsed.template_extras}")
    results["empty_extras"] = {"ok": True, "audit": audit, "usage": usage}

    # ----- 3. One template -----
    _print_section("Test 3: One template (pet addendum)")
    PetModel = _make_dynamic_model({"pet_addendum": _make_pet_extras_model()})
    audit = _audit_schema(PetModel)
    print(f"  schema audit: {audit}")
    parsed, usage = _call(client, PetModel, LEASE_NOTES)
    extras = parsed.template_extras
    print(f"  ✓ core: addr={parsed.property.address!r}, rent={parsed.monthly_rent!r}")
    if extras:
        pet = getattr(extras, "pet_addendum", None)
        print(f"  ✓ pet_addendum: name={getattr(pet, 'pet_name', None)!r}, breed={getattr(pet, 'pet_breed', None)!r}, deposit={getattr(pet, 'pet_deposit', None)!r}")
    else:
        print("  ⚠ template_extras is None")
    print(f"  usage: {usage}")
    results["one_template"] = {"ok": extras is not None, "audit": audit, "usage": usage}

    # ----- 4. Two templates -----
    _print_section("Test 4: Two templates (pet + pool)")
    BothModel = _make_dynamic_model({
        "pet_addendum": _make_pet_extras_model(),
        "pool_disclosure": _make_pool_extras_model(),
    })
    audit = _audit_schema(BothModel)
    print(f"  schema audit: {audit}")
    parsed, usage = _call(client, BothModel, LEASE_NOTES)
    extras = parsed.template_extras
    if extras:
        pet = getattr(extras, "pet_addendum", None)
        pool = getattr(extras, "pool_disclosure", None)
        print(f"  ✓ pet: {pet.model_dump() if pet else None}")
        print(f"  ✓ pool: {pool.model_dump() if pool else None}")
    print(f"  usage: {usage}")
    results["two_templates"] = {"ok": extras is not None, "audit": audit, "usage": usage}

    # ----- 5. Cache behavior: 3 calls same shape -----
    _print_section("Test 5: Cache behavior — same shape 3x then different shape")
    StableModel = BothModel
    cache_results = []
    for i in range(3):
        _, usage = _call(client, StableModel, LEASE_NOTES_NO_EXTRAS)
        print(f"  call {i+1} (same shape):  cached_tokens={usage['cached_tokens']}, input={usage['input_tokens']}")
        cache_results.append(("same", usage["cached_tokens"]))

    # Now a different shape — does cache reset?
    DifferentModel = _make_dynamic_model({"pet_addendum": _make_pet_extras_model()})
    _, usage = _call(client, DifferentModel, LEASE_NOTES_NO_EXTRAS)
    print(f"  call 4 (different shape): cached_tokens={usage['cached_tokens']}, input={usage['input_tokens']}")
    cache_results.append(("different", usage["cached_tokens"]))

    results["cache"] = {"results": cache_results}

    # ----- Verdict -----
    _print_section("VERDICT")
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
