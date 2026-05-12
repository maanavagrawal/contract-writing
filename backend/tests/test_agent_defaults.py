"""
Tests for per-agent defaults (workstream C) + voice transcription (workstream B).

Three concerns covered here:
  - /api/me/defaults GET/PUT/DELETE: allow-list, idempotent delete,
    cross-tenant isolation (the security gap flagged in plan-eng-review)
  - defaults merge inside /api/extract: precedence is
    extracted > default > null
  - /api/transcribe: 5MB cap, 100s duration cap, daily quota cap,
    request_id idempotency
"""
from __future__ import annotations

import io

import pytest

from backend import agent_defaults as defaults_mod
from backend import main as main_mod


# ============================================================
# /api/me/defaults — GET / PUT / DELETE
# ============================================================

def test_get_defaults_empty_for_new_user(authed_client):
    """New users get a clean empty dict + the full allow-list."""
    r = authed_client.get("/api/me/defaults")
    assert r.status_code == 200
    body = r.json()
    assert body["defaults"] == {}
    assert set(body["allowed_paths"]) == defaults_mod.ALLOWED_DEFAULT_PATHS


def test_put_default_allowed_path_succeeds(authed_client):
    r = authed_client.put(
        "/api/me/defaults/escrowee",
        json={"value": "seller"},
    )
    assert r.status_code == 200

    # Round-trip via GET to confirm persistence.
    r = authed_client.get("/api/me/defaults")
    assert r.json()["defaults"] == {"escrowee": "seller"}


def test_put_default_disallowed_path_returns_400(authed_client):
    """Random user-supplied path must be rejected — not silently dropped."""
    r = authed_client.put(
        "/api/me/defaults/property.address",
        json={"value": "221 W Hubbard"},
    )
    assert r.status_code == 400
    assert "not eligible" in r.json()["detail"]


def test_put_default_empty_value_returns_400(authed_client):
    """Empty value would silently shadow extracted values; reject so callers
    use DELETE explicitly."""
    r = authed_client.put(
        "/api/me/defaults/escrowee",
        json={"value": ""},
    )
    assert r.status_code == 400


def test_put_default_upserts_on_second_call(authed_client):
    """PUT same path twice — latest value wins, created_at preserved."""
    authed_client.put("/api/me/defaults/escrowee", json={"value": "seller"})
    authed_client.put("/api/me/defaults/escrowee", json={"value": "buyer"})

    r = authed_client.get("/api/me/defaults")
    assert r.json()["defaults"]["escrowee"] == "buyer"


def test_delete_default_removes_row(authed_client):
    authed_client.put("/api/me/defaults/escrowee", json={"value": "seller"})

    r = authed_client.delete("/api/me/defaults/escrowee")
    assert r.status_code == 204

    r = authed_client.get("/api/me/defaults")
    assert r.json()["defaults"] == {}


def test_delete_default_is_idempotent(authed_client):
    """Second DELETE on a gone row is fine — same 204 either way."""
    r = authed_client.delete("/api/me/defaults/escrowee")
    assert r.status_code == 204
    r = authed_client.delete("/api/me/defaults/escrowee")
    assert r.status_code == 204


# ---- CRITICAL: cross-tenant defaults isolation ----

def test_cross_tenant_defaults_are_isolated(two_authed_clients):
    """Alice's PUT must not affect Bob's defaults, and vice versa.

    REGRESSION TARGET (plan-eng-review failure mode #1): the agent_defaults
    table is keyed by (user_id, field_path) so any code path that forgets
    to include user_id in the WHERE clause would mix tenants. This test
    locks that behavior."""
    alice, bob = two_authed_clients

    alice.put("/api/me/defaults/escrowee", json={"value": "seller"})
    bob.put("/api/me/defaults/escrowee", json={"value": "buyer"})

    # Each user sees only their own value.
    assert alice.get("/api/me/defaults").json()["defaults"] == {"escrowee": "seller"}
    assert bob.get("/api/me/defaults").json()["defaults"] == {"escrowee": "buyer"}

    # Alice deleting her value MUST NOT touch Bob's row.
    alice.delete("/api/me/defaults/escrowee")
    assert alice.get("/api/me/defaults").json()["defaults"] == {}
    assert bob.get("/api/me/defaults").json()["defaults"] == {"escrowee": "buyer"}


def test_cross_tenant_nested_path_isolation(two_authed_clients):
    """The same security check on a nested agent.* path."""
    alice, bob = two_authed_clients

    alice.put("/api/me/defaults/agent.brokerage", json={"value": "Compass IL"})
    bob.put("/api/me/defaults/agent.brokerage", json={"value": "@properties"})

    assert alice.get("/api/me/defaults").json()["defaults"] == {"agent.brokerage": "Compass IL"}
    assert bob.get("/api/me/defaults").json()["defaults"] == {"agent.brokerage": "@properties"}


# ============================================================
# defaults merge inside /api/extract
# ============================================================

def test_extract_merges_defaults_into_empty_slots(authed_client, monkeypatch):
    """When the AI omits a field that the agent has set as a default,
    the default fills in and is reported in _defaults_applied."""
    authed_client.put("/api/me/defaults/escrowee", json={"value": "seller"})

    async def fake_extract(notes, images=None, template_extras=None, tier="full"):
        from backend.schema import TransactionFields
        # AI returns a sale with no escrowee — the slot is empty.
        return TransactionFields.model_validate({
            "transaction_type": "sale",
            "property": {"address": "221 W Hubbard"},
        })

    monkeypatch.setattr(main_mod, "extract_fields", fake_extract)

    r = authed_client.post("/api/extract", data={"notes": "221 W Hubbard $725k"})
    assert r.status_code == 200
    body = r.json()
    assert body["escrowee"] == "seller"
    assert body.get("_defaults_applied") == ["escrowee"]


def test_extract_extracted_value_wins_over_default(authed_client, monkeypatch):
    """Precedence: when the AI returns a value, the default never overrides it."""
    authed_client.put("/api/me/defaults/escrowee", json={"value": "seller"})

    async def fake_extract(notes, images=None, template_extras=None, tier="full"):
        from backend.schema import TransactionFields
        return TransactionFields.model_validate({
            "transaction_type": "sale",
            "property": {"address": "221 W Hubbard"},
            "escrowee": "buyer",
        })

    monkeypatch.setattr(main_mod, "extract_fields", fake_extract)

    r = authed_client.post("/api/extract", data={"notes": "deal notes"})
    body = r.json()
    assert body["escrowee"] == "buyer"  # extracted wins
    assert "_defaults_applied" not in body  # nothing was filled from defaults


def test_extract_nested_default_creates_intermediate_dicts(authed_client, monkeypatch):
    """agent.brokerage default must reach result['agent']['brokerage'] even
    if the AI's response had no 'agent' key at all."""
    authed_client.put("/api/me/defaults/agent.brokerage", json={"value": "Compass IL"})

    async def fake_extract(notes, images=None, template_extras=None, tier="full"):
        from backend.schema import TransactionFields
        return TransactionFields.model_validate({"transaction_type": "sale"})

    monkeypatch.setattr(main_mod, "extract_fields", fake_extract)

    r = authed_client.post("/api/extract", data={"notes": "x"})
    body = r.json()
    # TransactionFields.model_dump may omit "agent" entirely. Our merge must
    # synthesize it so the default lands somewhere readable downstream.
    assert body.get("agent", {}).get("brokerage") == "Compass IL"


def test_extract_tier_live_accepts_no_images(authed_client, monkeypatch):
    """Live tier silently drops images (debounced typing has none anyway).
    Smoke check that the tier field is plumbed through to extract_fields."""
    captured = {}

    async def fake_extract(notes, images=None, template_extras=None, tier="full"):
        captured["tier"] = tier
        from backend.schema import TransactionFields
        return TransactionFields.model_validate({"transaction_type": "sale"})

    monkeypatch.setattr(main_mod, "extract_fields", fake_extract)

    r = authed_client.post(
        "/api/extract",
        data={"notes": "deal", "tier": "live"},
    )
    assert r.status_code == 200
    assert captured["tier"] == "live"


def test_extract_invalid_tier_returns_400(authed_client):
    r = authed_client.post(
        "/api/extract",
        data={"notes": "x", "tier": "blazing-fast"},
    )
    assert r.status_code == 400


# ============================================================
# /api/transcribe — voice (workstream B)
# ============================================================

def test_transcribe_rejects_non_audio_content_type(authed_client):
    r = authed_client.post(
        "/api/transcribe",
        files={"audio": ("clip.txt", b"hello", "text/plain")},
        data={"request_id": "req-1", "duration_seconds": "5"},
    )
    assert r.status_code == 400


def test_transcribe_rejects_oversize_body(authed_client):
    big = b"x" * (defaults_mod.WHISPER_MAX_AUDIO_BYTES + 1)
    r = authed_client.post(
        "/api/transcribe",
        files={"audio": ("clip.webm", big, "audio/webm")},
        data={"request_id": "req-2", "duration_seconds": "5"},
    )
    assert r.status_code == 413


def test_transcribe_rejects_overlong_duration(authed_client):
    r = authed_client.post(
        "/api/transcribe",
        files={"audio": ("clip.webm", b"x", "audio/webm")},
        data={
            "request_id": "req-3",
            "duration_seconds": str(defaults_mod.WHISPER_MAX_AUDIO_SECONDS + 1),
        },
    )
    assert r.status_code == 422


def test_transcribe_daily_quota_rejects_over_cap(authed_client, monkeypatch):
    """Push the user past the daily seconds cap and verify the next request
    returns 429 BEFORE the Whisper call runs."""
    from backend import db
    with db.get_conn() as conn:
        defaults_mod.record_usage(
            conn,
            authed_client.user_id,
            defaults_mod.WHISPER_DAILY_SECONDS_CAP - 5,
        )

    # 10s request would push us 5s over the cap.
    r = authed_client.post(
        "/api/transcribe",
        files={"audio": ("clip.webm", b"x", "audio/webm")},
        data={"request_id": "req-cap", "duration_seconds": "10"},
    )
    assert r.status_code == 429
    assert "cap" in r.json()["detail"].lower()


def test_transcribe_idempotent_request_id_returns_cached(authed_client, monkeypatch):
    """Same (user, request_id) within 60s returns the cached transcript and
    skips the Whisper call entirely — no double billing."""
    from backend import db

    # Pre-seed an idempotency entry as if a prior call already succeeded.
    with db.get_conn() as conn:
        defaults_mod.cache_transcript(
            conn,
            authed_client.user_id,
            "req-replay",
            "cached transcript text",
        )

    # If Whisper got called, this would raise — but it should not be called.
    def boom(*a, **kw):
        raise AssertionError("Whisper called on cached idempotent retry")

    from openai import OpenAI
    monkeypatch.setattr(OpenAI, "audio", property(lambda self: type("X", (), {"transcriptions": type("T", (), {"create": boom})()})()))

    r = authed_client.post(
        "/api/transcribe",
        files={"audio": ("clip.webm", b"xxxx", "audio/webm")},
        data={"request_id": "req-replay", "duration_seconds": "5"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["transcript"] == "cached transcript text"
    assert body["cached"] is True


# ============================================================
# /api/extract/stream — SSE chip events (workstream A)
# ============================================================

def test_stream_emits_chip_events_for_filled_fields(authed_client, monkeypatch):
    """Each canonical chip field with a value becomes a chip event; empty
    slots are skipped; final 'done' event carries the whole payload."""
    async def fake_extract(notes, images=None, template_extras=None, tier="full"):
        from backend.schema import TransactionFields
        return TransactionFields.model_validate({
            "transaction_type": "sale",
            "property": {"address": "221 W Hubbard", "unit": "803"},
            "purchase_price": "725000",
            # closing_date deliberately omitted → no chip event for it.
        })

    monkeypatch.setattr(main_mod, "extract_fields", fake_extract)

    with authed_client.stream(
        "POST", "/api/extract/stream", data={"notes": "deal", "tier": "live"}
    ) as r:
        assert r.status_code == 200
        body = b"".join(r.iter_bytes()).decode("utf-8")

    # Required envelope events.
    assert "event: started" in body
    assert "event: done" in body

    # Chips actually emitted for the present fields, in order.
    chip_events = [line for line in body.splitlines() if line.startswith("data:") and '"path"' in line]
    paths = []
    for ev in chip_events:
        import json as _json
        d = _json.loads(ev.removeprefix("data:").strip())
        paths.append(d["path"])
    assert "property.address" in paths
    assert "property.unit" in paths
    assert "transaction_type" in paths
    assert "purchase_price" in paths
    assert "closing_date" not in paths  # was omitted → no chip


def test_stream_rate_limit_rejects_after_cap(authed_client, monkeypatch):
    """REGRESSION (code-review 2026-05-11): a runaway client could spam the
    live tier and burn dollars on gpt-5-mini. /api/extract/stream now
    rate-limits at LIVE_EXTRACT_PER_MINUTE_CAP per user-minute and returns
    429 on overage — the OpenAI call is never made past the cap."""
    from backend import db

    # Pre-record the cap so the next request trips the limit.
    with db.get_conn() as conn:
        for _ in range(defaults_mod.LIVE_EXTRACT_PER_MINUTE_CAP):
            defaults_mod.check_and_record_extract(conn, authed_client.user_id)

    # Boom — if extract_fields gets called, fail.
    def boom(*a, **kw):
        raise AssertionError("extract_fields should not run when rate-limited")
    monkeypatch.setattr(main_mod, "extract_fields", boom)

    with authed_client.stream(
        "POST", "/api/extract/stream", data={"notes": "x", "tier": "live"}
    ) as r:
        assert r.status_code == 429


def test_stream_marks_defaults_with_source_default(authed_client, monkeypatch):
    """A chip filled from agent_defaults must carry source=default so the
    frontend can render the passive tick. Extracted chips get source=extracted."""
    authed_client.put("/api/me/defaults/escrowee", json={"value": "seller"})

    async def fake_extract(notes, images=None, template_extras=None, tier="full"):
        from backend.schema import TransactionFields
        return TransactionFields.model_validate({
            "transaction_type": "sale",
            "property": {"address": "X"},
        })

    monkeypatch.setattr(main_mod, "extract_fields", fake_extract)

    with authed_client.stream(
        "POST", "/api/extract/stream", data={"notes": "x", "tier": "live"}
    ) as r:
        body = b"".join(r.iter_bytes()).decode("utf-8")

    import json as _json
    found_escrowee = None
    found_addr = None
    for line in body.splitlines():
        if line.startswith("data:") and '"path"' in line:
            d = _json.loads(line.removeprefix("data:").strip())
            if d["path"] == "escrowee":
                found_escrowee = d
            if d["path"] == "property.address":
                found_addr = d

    assert found_escrowee is not None
    assert found_escrowee["source"] == "default"
    assert found_addr is not None
    assert found_addr["source"] == "extracted"


def test_transcribe_idempotency_pk_is_composite_user_id(two_authed_clients):
    """REGRESSION (code-review 2026-05-11): when whisper_idempotency PK was
    global on request_id, user B caching with a request_id user A was
    already holding would silently no-op (ON CONFLICT DO NOTHING), so B's
    OWN retries never hit the cache and re-billed every time. Now the PK is
    (user_id, request_id), so each user owns their own cache row even on
    collision."""
    from backend import db
    alice, bob = two_authed_clients

    # Both users cache a transcript under the same request_id.
    with db.get_conn() as conn:
        defaults_mod.cache_transcript(conn, alice.user_id, "shared-id", "alice text")
        defaults_mod.cache_transcript(conn, bob.user_id, "shared-id", "bob text")
        alice_cached = defaults_mod.get_cached_transcript(conn, alice.user_id, "shared-id")
        bob_cached = defaults_mod.get_cached_transcript(conn, bob.user_id, "shared-id")

    # Both users see THEIR OWN transcript on retry, regardless of who got there first.
    assert alice_cached == "alice text"
    assert bob_cached == "bob text"


def test_transcribe_cross_tenant_idempotency_isolation(two_authed_clients):
    """Bob's request_id collision with Alice's MUST NOT leak Alice's
    transcript to Bob. The idempotency lookup is keyed by (user, request_id),
    not request_id alone."""
    from backend import db
    alice, bob = two_authed_clients

    with db.get_conn() as conn:
        defaults_mod.cache_transcript(conn, alice.user_id, "shared-id", "alice's secret notes")

    # Bob asks for the same request_id with no prior transcript of his own.
    # This MUST NOT return Alice's transcript. Since Bob has no cache hit
    # and we don't have a real OPENAI_API_KEY in tests, we expect a 500/502
    # — never 200 with Alice's text.
    r = bob.post(
        "/api/transcribe",
        files={"audio": ("clip.webm", b"xxxx", "audio/webm")},
        data={"request_id": "shared-id", "duration_seconds": "5"},
    )
    assert r.status_code != 200 or "alice's secret notes" not in r.json().get("transcript", "")
