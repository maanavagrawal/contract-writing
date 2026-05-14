"""
Per-agent default field values + Whisper-usage tracking.

Two concerns, one module because they share a multi-tenant shape (every
read/write is scoped to a user_id) and they're both new in this feature.
Keeping them out of models.py prevents that file from growing into a junk
drawer.

# agent_defaults
The allow-list defines which canonical field paths an agent can save as a
default. New entries get a one-line rationale in the comment so anyone
extending this list has the same context.

# whisper_usage + whisper_idempotency
Per-day audio seconds and request counts for cost control, plus an
idempotency cache so a network retry within 60s gets the cached transcript
instead of double-billing the user. Opportunistic cleanup of old
idempotency rows on each insert keeps the table tiny at our scale.
"""
from __future__ import annotations

from datetime import date as date_cls, datetime, timedelta, timezone

import psycopg

# Allow-list: only these paths can be saved as defaults. Anything else returns
# 400 from the API. Keeps users from saving deal-specific values (price,
# closing date, address) by accident.
#
# Why each path:
#   escrowee                       — agent's brokerage usually serves as escrowee
#   loan_amortization_years        — 30y is the IL default; pre-fill saves a tap
#   loan_max_points                — agent's standard cap (often 1.0)
#   loan_percent_of_price          — agent's typical LTV (often 80%)
#   tax_proration_percent          — Cook County standard is 110%
#   earnest_business_days          — most agents use a fixed number (3 or 5)
#   protection_period_days         — lease standard (30 days)
#   agent.brokerage                — agent's brokerage name
#   agent.brokerage_address        — agent's brokerage street address
#   agent.brokerage_mls            — agent's MLS ID
#   agent.brokerage_license        — brokerage license #
#   agent.mls                      — agent's individual MLS ID
#   agent.license                  — agent's individual license #
#   agent.email_signature          — for documents that include it (templates)
#   agent.signature                — base64 PNG, stamped onto "By (Broker/Agent)"
#                                    rows during fill. Set via the signature
#                                    capture modal. See pdf_fill._stamp_signature.
#   agent.initials                 — base64 PNG, stamped onto "Agent's Initials"
#                                    boxes. Captured alongside agent.signature in
#                                    the same modal.
ALLOWED_DEFAULT_PATHS: frozenset[str] = frozenset({
    "escrowee",
    "loan_amortization_years",
    "loan_max_points",
    "loan_percent_of_price",
    "tax_proration_percent",
    "earnest_business_days",
    "protection_period_days",
    "agent.brokerage",
    "agent.brokerage_address",
    "agent.brokerage_mls",
    "agent.brokerage_license",
    "agent.mls",
    "agent.license",
    "agent.email_signature",
    "agent.signature",
    "agent.initials",
})

# Paths whose value is a base64-encoded PNG, not free text. The API gate caps
# their size at MAX_BLOB_VALUE_BYTES (~200KB base64 ≈ 150KB binary) to keep a
# misbehaving client from flooding agent_defaults with a 50MB PNG and slowing
# down every list_defaults call for that user. Postgres TOAST handles small
# blobs cleanly; the cap is operational paranoia, not a storage limit.
BLOB_VALUED_PATHS: frozenset[str] = frozenset({
    "agent.signature",
    "agent.initials",
})
MAX_BLOB_VALUE_BYTES: int = 200 * 1024

# List-typed top-level fields in TransactionFields. Adding ANY of these to
# ALLOWED_DEFAULT_PATHS would let _merge_defaults_into clobber an extracted
# list with a dict — silent corruption. Hard-reject at the gate so a future
# allow-list edit can't introduce this bug without also touching the
# merge logic.
_LIST_VALUED_ROOTS: frozenset[str] = frozenset({
    "tenant_or_buyer_names",
    "seller_names",
})


def is_allowed_default_path(path: str) -> bool:
    """API-layer gate. Returns False for any path not in ALLOWED_DEFAULT_PATHS.

    Belt-and-suspenders check: also rejects any path whose first segment is a
    known list-valued field. _merge_defaults_into walks dotted paths as
    nested dicts and would silently clobber a list with a dict — guarding
    here keeps that whole class of bug out of the surface area, even if a
    future edit adds (e.g.) 'tenant_or_buyer_names.0' to the allow-list."""
    if path not in ALLOWED_DEFAULT_PATHS:
        return False
    root = path.split(".", 1)[0]
    if root in _LIST_VALUED_ROOTS:
        return False
    return True


def list_defaults(conn: psycopg.Connection, user_id: str) -> dict[str, str]:
    """All defaults for one user as a flat {field_path: value} dict. Empty
    dict if the user has none. Single query — no N+1 risk on extract merge."""
    rows = conn.execute(
        "SELECT field_path, value FROM agent_defaults WHERE user_id = %s",
        (user_id,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def upsert_default(
    conn: psycopg.Connection,
    user_id: str,
    field_path: str,
    value: str,
) -> None:
    """Set or replace a default. PK is (user_id, field_path) so ON CONFLICT
    handles the update branch naturally — callers don't need a check-then-
    insert (which would have a race) or a DELETE-first (which would lose the
    created_at). updated_at always advances; created_at sticks."""
    conn.execute(
        """
        INSERT INTO agent_defaults (user_id, field_path, value, created_at, updated_at)
        VALUES (%s, %s, %s, NOW(), NOW())
        ON CONFLICT (user_id, field_path)
        DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """,
        (user_id, field_path, value),
    )


def delete_default(
    conn: psycopg.Connection,
    user_id: str,
    field_path: str,
) -> int:
    """Remove one default. Returns rows-affected so the API can decide between
    200 and 404 — though we treat both the same to keep DELETE idempotent
    (second DELETE on a gone row is fine, the result is the same)."""
    cur = conn.execute(
        "DELETE FROM agent_defaults WHERE user_id = %s AND field_path = %s",
        (user_id, field_path),
    )
    return cur.rowcount or 0


# ---- Whisper usage / idempotency ----

# Defaults; kept module-level so tests + the API can monkeypatch without
# threading env vars through every call site.
WHISPER_DAILY_SECONDS_CAP = 600   # 10 minutes per agent per day
WHISPER_MAX_AUDIO_BYTES = 5 * 1024 * 1024   # 5 MB hard cap (~3-5 min of opus)
WHISPER_MAX_AUDIO_SECONDS = 100   # 90s plan + small headroom for clock skew
WHISPER_IDEMPOTENCY_TTL_SECONDS = 60   # retry window after which request_id is forgotten

# Per-user minute-window cap for live extraction. Frontend already debounces
# at 1.2s + 30-char diff threshold, but a misbehaving client (buggy retry
# loop, forgotten background tab) could still burn dollars on gpt-5-mini.
# 8/min = 1 per 7.5s — comfortably above the debounce cadence for any real
# user, comfortably below "runaway script."
LIVE_EXTRACT_PER_MINUTE_CAP = 8


def get_today_usage(conn: psycopg.Connection, user_id: str) -> tuple[int, int]:
    """Return (seconds_used_today, requests_today) for this user. Zero if no
    row exists yet. Used both by the API (to enforce the cap) and by tests."""
    row = conn.execute(
        """
        SELECT seconds, requests
        FROM whisper_usage
        WHERE user_id = %s AND day = CURRENT_DATE
        """,
        (user_id,),
    ).fetchone()
    if not row:
        return (0, 0)
    return (int(row[0] or 0), int(row[1] or 0))


def record_usage(
    conn: psycopg.Connection,
    user_id: str,
    seconds_added: int,
) -> None:
    """Increment today's usage row. UPSERT pattern so the first request of the
    day works without a separate INSERT path. Adds 1 to request count and
    `seconds_added` to seconds — caller passes the audio length they billed."""
    conn.execute(
        """
        INSERT INTO whisper_usage (user_id, day, seconds, requests)
        VALUES (%s, CURRENT_DATE, %s, 1)
        ON CONFLICT (user_id, day)
        DO UPDATE SET
            seconds = whisper_usage.seconds + EXCLUDED.seconds,
            requests = whisper_usage.requests + 1
        """,
        (user_id, max(0, int(seconds_added))),
    )


def get_cached_transcript(
    conn: psycopg.Connection,
    user_id: str,
    request_id: str,
) -> str | None:
    """Return the previously-computed transcript for this (user, request_id)
    pair if it was processed within the idempotency window. The user_id check
    is critical: an attacker who guessed another user's request_id must not
    receive their transcript."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=WHISPER_IDEMPOTENCY_TTL_SECONDS)
    row = conn.execute(
        """
        SELECT transcript
        FROM whisper_idempotency
        WHERE request_id = %s AND user_id = %s AND created_at > %s
        """,
        (request_id, user_id, cutoff),
    ).fetchone()
    return row[0] if row else None


def check_and_record_extract(
    conn: psycopg.Connection,
    user_id: str,
) -> bool:
    """Atomic rate-limit gate for /api/extract/stream live tier.

    Returns True if the call is within the per-minute cap (and records it),
    False if it would exceed the cap. The window is the current UTC minute —
    coarse, but enough to stop a misbehaving client from burning hundreds of
    gpt-5-mini calls/hour.

    Implementation: one row per (user_id, minute), incremented via UPSERT.
    Old rows fall off naturally — we don't even need cleanup at our scale
    (a year of one-user data is < 525k rows). Revisit if scale changes.
    """
    row = conn.execute(
        """
        INSERT INTO extract_usage (user_id, minute, count)
        VALUES (%s, date_trunc('minute', NOW()), 1)
        ON CONFLICT (user_id, minute)
        DO UPDATE SET count = extract_usage.count + 1
        RETURNING count
        """,
        (user_id,),
    ).fetchone()
    if not row:
        return True
    return int(row[0]) <= LIVE_EXTRACT_PER_MINUTE_CAP


def cache_transcript(
    conn: psycopg.Connection,
    user_id: str,
    request_id: str,
    transcript: str,
) -> None:
    """Store a transcript for idempotency. Opportunistic cleanup of expired
    rows on every insert keeps the table bounded without a background job —
    cheap at our scale (< 1k rows), revisit if scale changes.

    On conflict (same user + request_id reposted): keep the original — never
    overwrite, since the response was already returned to the client. PK is
    (user_id, request_id) so two different users with the same request_id
    each get their own cache row (see migration 0007)."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=WHISPER_IDEMPOTENCY_TTL_SECONDS)
    conn.execute("DELETE FROM whisper_idempotency WHERE created_at < %s", (cutoff,))
    conn.execute(
        """
        INSERT INTO whisper_idempotency (request_id, user_id, transcript)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id, request_id) DO NOTHING
        """,
        (request_id, user_id, transcript),
    )
