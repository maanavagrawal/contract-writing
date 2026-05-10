"""
Magic-link authentication.

Two tables (see migrations/0002_users_and_sessions.sql):
  users        — identity is email; pending_token_hash holds the SHA-256 of
                 the most recent unredeemed magic-link.
  sessions     — one row per logged-in browser; token_hash is SHA-256 of the
                 session-cookie value.

Plaintext tokens never touch the database. The DB stores only SHA-256
hashes, so a read-only DB compromise can't impersonate users.

Token lifecycle:
  POST /api/auth/login {email}                  →  send_magic_link(email)
                                                    - upsert user row by email
                                                    - generate 32-byte token
                                                    - store token_hash + 15min expiry
                                                    - rate limit: 3 sends/hour per user
                                                    - email magic-link to user
                                                  →  always 200 (no enumeration)

  GET  /auth/redeem?token=...                   →  HTML "Click to log in" page
                                                    (defeats email prefetchers)

  POST /api/auth/redeem {token}                 →  redeem_token(token)
                                                    - SELECT FOR UPDATE on users row
                                                    - reject expired/used/malformed
                                                    - clear pending_token_*
                                                    - INSERT session row + set cookie
                                                    - 14-day expiry

  POST /api/auth/logout                         →  delete session row, clear cookie

Session cookies are HttpOnly, Secure (in prod), SameSite=Lax, Max-Age=14d.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg
from fastapi import Cookie, HTTPException, Request, Response

from . import email_send, models

logger = logging.getLogger(__name__)

# Cookie name for the session token. Short on purpose so it doesn't leak app
# branding in random network traces.
SESSION_COOKIE = "msid"

# Magic-link tokens valid for 15 minutes. Long enough that an iPhone email-
# delay won't cause failure; short enough that an unsuspecting copy-paste of
# the URL into a chat doesn't expose an indefinite session-creator.
MAGIC_LINK_TTL = timedelta(minutes=15)

# Sessions valid for 14 days. Real-estate agents use this app a few times a
# week — 2 weeks balances "don't make me re-login often" against "stolen
# laptop session can't roam forever."
SESSION_TTL = timedelta(days=14)

# Per-user rate limit: 3 magic-link sends per user per rolling hour. Defeats
# the "spam one inbox by repeating /api/auth/login" attack without
# inconveniencing real users (who rarely need more than 1).
RATE_LIMIT_WINDOW = timedelta(hours=1)
RATE_LIMIT_MAX_SENDS = 3

# Global rate limit: total magic-link sends across all emails in a rolling
# hour. The per-user limit doesn't help when an attacker uses unique fresh
# emails on every request — each new email gets its own clean counter. This
# global cap is the backstop that bounds total Resend spend regardless of
# how many emails the attacker burns through. 50/hr is generous: two real
# users on the same morning still have plenty of headroom.
GLOBAL_RATE_LIMIT_WINDOW = timedelta(hours=1)
GLOBAL_RATE_LIMIT_MAX_SENDS = 50


# ---------- Errors ----------

class AuthError(HTTPException):
    """Auth failures map to 401 with a generic message. We deliberately don't
    distinguish "expired" from "used" from "malformed" in the public response
    so attackers can't probe the token namespace."""

    def __init__(self, detail: str = "invalid or expired token"):
        super().__init__(status_code=401, detail=detail)


class RateLimitError(HTTPException):
    def __init__(self):
        super().__init__(status_code=429, detail="too many requests; try again later")


# ---------- User model ----------

@dataclass
class User:
    """Authenticated user, returned by the current_user dependency. Kept
    minimal — the rest of models.py owns the persistence shape; this is just
    what handlers need to do permission checks."""
    id: str
    email: str


# ---------- Helpers ----------

def _hash_token(plaintext: str) -> str:
    """SHA-256 the token. Constant-time comparison happens via hmac.compare_digest
    when we look up by hash (Postgres '=' isn't constant-time, but the hashes
    are the only inputs the attacker controls and we're comparing against
    DB values they can't see)."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_email(email: str) -> str:
    """Email comparison is case-insensitive at the local-part on most providers
    in practice. We store the casing the user typed (for display) and match
    via LOWER(email)."""
    return email.strip()


def _is_secure_request(request: Request) -> bool:
    """Set the Secure cookie attribute when the request was over HTTPS. In
    local dev (HTTP) we drop Secure so the cookie actually gets sent back.
    Honors X-Forwarded-Proto for proxies (Railway terminates TLS upstream)."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto == "https"


# ---------- Public API: magic-link send ----------

def _check_and_bump_global_rate_limit(conn: psycopg.Connection, now: datetime) -> None:
    """Atomically increment the global send counter or raise RateLimitError.

    Single row keyed on `'global_magic_link'`; we lock it FOR UPDATE inside the
    caller's transaction so concurrent requests serialize at this point. Window
    resets when the recorded start is older than GLOBAL_RATE_LIMIT_WINDOW.

    Migration 0003 seeds the row, so the SELECT always finds it.
    """
    row = conn.execute(
        "SELECT send_count, window_start FROM auth_rate_limit "
        "WHERE id = 'global_magic_link' FOR UPDATE"
    ).fetchone()
    if row is None:
        # Defensive: migration 0003 should have seeded this. If it didn't,
        # treat as a fresh window so we don't lock the user out indefinitely.
        conn.execute(
            "INSERT INTO auth_rate_limit (id, send_count, window_start) "
            "VALUES ('global_magic_link', 0, %s) ON CONFLICT (id) DO NOTHING",
            (now,),
        )
        send_count, window_start = 0, now
    else:
        send_count, window_start = row

    if window_start is None or now - window_start > GLOBAL_RATE_LIMIT_WINDOW:
        send_count = 0
        window_start = now
    send_count += 1

    if send_count > GLOBAL_RATE_LIMIT_MAX_SENDS:
        raise RateLimitError()

    conn.execute(
        "UPDATE auth_rate_limit SET send_count = %s, window_start = %s "
        "WHERE id = 'global_magic_link'",
        (send_count, window_start),
    )


def send_magic_link(conn: psycopg.Connection, email: str, base_url: str) -> None:
    """Issue a magic link, persist its hash, and send the email.

    Always returns successfully on the no-op paths (invalid email, rate-limited)
    so the API caller (POST /api/auth/login) returns 200 regardless of whether
    `email` is a known user. This is the email-enumeration defense.

    Atomicity guarantees:
      - Per-user rate limit + global rate limit + token write + email send
        all happen inside one transaction. If email send fails (Resend down,
        DNS unverified, quota exceeded), the entire transaction rolls back —
        the user's rate-limit counter is NOT burned for emails they never got.
      - First-time users for a given email use INSERT ... ON CONFLICT so two
        concurrent first-time logins don't trip the unique index. The losing
        side falls through to the existing-user UPDATE branch on the next loop.

    Raises:
      RateLimitError — per-user OR global cap exceeded.
      HTTPException(503) — email send failed; transaction rolled back.
    """
    email = _normalize_email(email)
    if not email or "@" not in email:
        logger.info("auth.send_magic_link: invalid email format, ignoring")
        return

    now = _now()
    plaintext = secrets.token_urlsafe(32)
    token_hash = _hash_token(plaintext)
    expires = now + MAGIC_LINK_TTL

    link = f"{base_url.rstrip('/')}/auth/redeem?token={plaintext}"
    subject = "Your memoir login link"
    html = (
        f"<p>Click below to log in to memoir:</p>"
        f'<p><a href="{link}" style="display:inline-block;padding:10px 16px;'
        f'background:#1f6feb;color:#fff;text-decoration:none;border-radius:6px;">'
        f"Log in to memoir</a></p>"
        f"<p>This link expires in 15 minutes. If you didn't request this email, "
        f"you can safely ignore it — no account changes have been made.</p>"
        f"<p style='color:#666;font-size:12px;'>{link}</p>"
    )

    with conn.transaction():
        # Global cap first: cheap, single-row lock, fails fast on abuse.
        _check_and_bump_global_rate_limit(conn, now)

        # Find or create the user.
        row = conn.execute(
            """
            SELECT id, token_request_count, token_request_window_start
            FROM users
            WHERE LOWER(email) = LOWER(%s)
            FOR UPDATE
            """,
            (email,),
        ).fetchone()

        if row is None:
            # New user. ON CONFLICT handles the concurrent-first-login race:
            # two requests both see no row, both try to INSERT — without
            # ON CONFLICT, the loser hits idx_users_email_lower and 500s.
            # With it, the loser inserts nothing, then we re-SELECT to grab
            # the row the winner just wrote and treat this request as an
            # existing-user request (counter starts at 1 either way).
            user_id = models.new_id()
            inserted = conn.execute(
                """
                INSERT INTO users (id, email, pending_token_hash,
                                   pending_token_expires,
                                   token_request_count,
                                   token_request_window_start)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (LOWER(email)) DO NOTHING
                """,
                (user_id, email, token_hash, expires, 1, now),
            )
            if inserted.rowcount == 0:
                # Lost the race. Re-SELECT and fall through to UPDATE below.
                row = conn.execute(
                    """
                    SELECT id, token_request_count, token_request_window_start
                    FROM users
                    WHERE LOWER(email) = LOWER(%s)
                    FOR UPDATE
                    """,
                    (email,),
                ).fetchone()
                # row must exist now — the winner inserted it. If it doesn't
                # (race-on-race + winner row got deleted before we got to
                # re-SELECT), surface as a real exception. Don't use `assert`
                # here — `python -O` strips asserts and leaves us with a
                # confusing tuple-unpack TypeError on the next line.
                if row is None:
                    raise RuntimeError(
                        "send_magic_link: ON CONFLICT re-SELECT found no row "
                        "for email after lost insert race"
                    )
                user_id, count, window_start = row
                if window_start is None or now - window_start > RATE_LIMIT_WINDOW:
                    count = 0
                    window_start = now
                count += 1
                if count > RATE_LIMIT_MAX_SENDS:
                    raise RateLimitError()
                conn.execute(
                    """
                    UPDATE users
                    SET pending_token_hash = %s,
                        pending_token_expires = %s,
                        token_request_count = %s,
                        token_request_window_start = %s
                    WHERE id = %s
                    """,
                    (token_hash, expires, count, window_start, user_id),
                )
        else:
            user_id, count, window_start = row

            if window_start is None or now - window_start > RATE_LIMIT_WINDOW:
                count = 0
                window_start = now
            count += 1

            if count > RATE_LIMIT_MAX_SENDS:
                raise RateLimitError()

            conn.execute(
                """
                UPDATE users
                SET pending_token_hash = %s,
                    pending_token_expires = %s,
                    token_request_count = %s,
                    token_request_window_start = %s
                WHERE id = %s
                """,
                (token_hash, expires, count, window_start, user_id),
            )

        # Email send is INSIDE the transaction. If it fails, the rollback
        # undoes the rate-limit bump and the token write — the user can retry
        # without burning a slot for an email they never received.
        try:
            email_send.send(to=email, subject=subject, html=html)
        except email_send.EmailSendError as e:
            # Re-raise as HTTPException so the caller sends 503. The
            # `with conn.transaction()` block intercepts the exception,
            # rolls back the open transaction, then re-raises.
            raise HTTPException(status_code=503, detail=f"could not send login email: {e}")


# ---------- Public API: redemption ----------

def redeem_token(conn: psycopg.Connection, plaintext: str) -> User:
    """Burn a magic-link token and return the authenticated User.

    Atomic via SELECT FOR UPDATE on the user row matching the token hash.
    Two concurrent redemptions of the same token: first wins, second sees
    the cleared pending_token and gets AuthError.

    The plaintext is the cookie-shaped value handed to the user via email.
    """
    if not plaintext:
        raise AuthError()

    token_hash = _hash_token(plaintext)
    now = _now()

    with conn.transaction():
        row = conn.execute(
            """
            SELECT id, email, pending_token_hash, pending_token_expires
            FROM users
            WHERE pending_token_hash = %s
            FOR UPDATE
            """,
            (token_hash,),
        ).fetchone()

        if row is None:
            raise AuthError()

        user_id, email, stored_hash, expires = row

        # Defense in depth — the WHERE already matched, but constant-time
        # compare keeps us honest if we ever change the lookup pattern.
        if not hmac.compare_digest(stored_hash or "", token_hash):
            raise AuthError()

        if expires is None or expires < now:
            # Clear the expired token so a refreshed window doesn't accept it.
            conn.execute(
                "UPDATE users SET pending_token_hash = NULL, pending_token_expires = NULL "
                "WHERE id = %s",
                (user_id,),
            )
            raise AuthError()

        # Burn the token — single use.
        conn.execute(
            "UPDATE users SET pending_token_hash = NULL, pending_token_expires = NULL "
            "WHERE id = %s",
            (user_id,),
        )

    return User(id=user_id, email=email)


def issue_session(conn: psycopg.Connection, user: User) -> str:
    """Create a session row and return the cookie plaintext. Caller sets the
    cookie on the response."""
    plaintext = secrets.token_urlsafe(32)
    token_hash = _hash_token(plaintext)
    expires = _now() + SESSION_TTL
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
        (token_hash, user.id, expires),
    )
    return plaintext


def revoke_session(conn: psycopg.Connection, plaintext: str) -> None:
    """Delete a session row by cookie plaintext. Idempotent — deleting a
    non-existent token is a no-op."""
    if not plaintext:
        return
    conn.execute(
        "DELETE FROM sessions WHERE token_hash = %s",
        (_hash_token(plaintext),),
    )


def set_session_cookie(response: Response, plaintext: str, *, secure: bool) -> None:
    """Standard cookie config: HttpOnly, SameSite=Lax, Secure on HTTPS only.
    Max-Age tracks SESSION_TTL so the browser drops the cookie when the
    server-side row would too."""
    response.set_cookie(
        SESSION_COOKIE,
        plaintext,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


# ---------- Dependency: current_user ----------

def current_user(
    request: Request,
    msid: str | None = Cookie(default=None),
) -> User:
    """FastAPI dependency. Resolves the session cookie to a User or raises
    AuthError. Mounted on every protected /api/* route.

    Cookies expire client-side at MAX_AGE, but a stale cookie can still
    arrive (clock skew, reused browser snapshot). We re-check expires_at
    server-side on every request so revocation is real-time.
    """
    if not msid:
        raise AuthError("not authenticated")

    token_hash = _hash_token(msid)

    from . import db
    with db.get_conn() as conn:
        row = conn.execute(
            """
            SELECT s.user_id, s.expires_at, u.email
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = %s
            """,
            (token_hash,),
        ).fetchone()

    if row is None:
        raise AuthError("not authenticated")

    user_id, expires_at, email = row
    if expires_at is None or expires_at < _now():
        # Don't bother deleting on read; logout/cleanup handles that. The
        # cookie will be re-set on next login.
        raise AuthError("session expired")

    return User(id=user_id, email=email)


# ---------- Helper for tests ----------

def _force_create_session(conn: psycopg.Connection, email: str) -> tuple[str, str]:
    """Test-only helper: ensure a user with this email exists, then issue a
    session and return (user_id, session_plaintext). Avoids round-tripping
    through the email-send + redeem flow in tests that aren't testing those
    code paths."""
    email = _normalize_email(email)
    row = conn.execute(
        "SELECT id FROM users WHERE LOWER(email) = LOWER(%s)",
        (email,),
    ).fetchone()
    if row is None:
        user_id = models.new_id()
        conn.execute(
            "INSERT INTO users (id, email) VALUES (%s, %s)",
            (user_id, email),
        )
    else:
        user_id = row[0]
    plaintext = issue_session(conn, User(id=user_id, email=email))
    return user_id, plaintext


def _resolve_base_url(request: Request) -> str:
    """Build the absolute URL prefix for the magic link. Honors APP_BASE_URL
    when set (production), otherwise uses the request's own host (dev)."""
    explicit = os.environ.get("APP_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"
