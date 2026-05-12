"""
Test infrastructure: a session-scoped Postgres container + per-test isolation.

We boot one Postgres container for the whole test session (Docker startup is
~3-5s, so paying that once is fine; per-test would multiply by N tests). Each
test gets a clean slate via TRUNCATE — faster than dropping/recreating
schema, and exercises the same migrated schema every test would see in prod.

If Docker isn't available, every test that depends on `db_session` is skipped
with a clear message. That keeps `pytest backend/tests/test_pdf_fill.py` (no
DB needed) running fine on a machine without Docker.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
# Ryuk (testcontainers' container reaper) sometimes can't bind 8080 on Docker
# Desktop / OrbStack setups. We don't need it for short-lived test runs — the
# session-scoped fixture's __exit__ tears down our PG container cleanly.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

# Hard-clear RESEND_API_KEY for tests. The auth suite assumes email sends
# are no-ops (email_send.send logs and returns when the key is unset). A
# dev .env with a real RESEND key would otherwise make every send_magic_link
# call hit the Resend API — slow, brittle, and a 503 on rate-limited or
# unverified-domain sends. The autouse fixture below defends against
# backend.main's load_dotenv() restoring the key at import time.
os.environ.pop("RESEND_API_KEY", None)


@pytest.fixture(autouse=True)
def _unset_resend_api_key():
    """backend/main.py calls load_dotenv() at import time, which may resurrect
    RESEND_API_KEY from .env after the top-of-module pop above. This autouse
    fixture re-pops on every test so the auth tests reliably see the dev
    "log and return" branch in email_send.send."""
    os.environ.pop("RESEND_API_KEY", None)
    yield


def _docker_available() -> bool:
    """Smoke-test docker. testcontainers raises an opaque error chain if
    Docker daemon is down; better to skip with a clear message.

    The python docker SDK defaults to /var/run/docker.sock but Mac users
    often run OrbStack or Docker Desktop with a per-user socket. Resolve via
    `docker context inspect` on first call so testcontainers picks up the
    same daemon the user's CLI talks to.
    """
    if "DOCKER_HOST" not in os.environ:
        try:
            import json as _json
            import subprocess
            out = subprocess.check_output(
                ["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"],
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            host = _json.loads(out.decode().strip())
            if host:
                os.environ["DOCKER_HOST"] = host
        except Exception:
            pass

    try:
        import docker
        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def postgres_container():
    """Boot one Postgres container for the test session. Yields the
    container; the DSN is wired into the env so backend.db picks it up."""
    if not _docker_available():
        pytest.skip("Docker daemon not running; skipping Postgres-backed tests")

    from testcontainers.postgres import PostgresContainer

    # Postgres 16 matches Railway's default. Pinning so a future testcontainers
    # default change doesn't quietly drift between dev and prod.
    with PostgresContainer("postgres:16-alpine") as pg:
        # testcontainers gives us a SQLAlchemy-style URL; psycopg wants a
        # plain libpq URL. They're identical for our purposes.
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        os.environ["TEST_DATABASE_URL"] = dsn

        from backend import db
        db.reset_pool_for_tests()
        db.run_migrations()

        yield pg

        db.reset_pool_for_tests()
        os.environ.pop("TEST_DATABASE_URL", None)


@pytest.fixture
def clean_db(postgres_container):
    """Truncate tables before each test for isolation. Fast (microseconds)
    and keeps the migrated schema intact.

    auth_rate_limit is a single-row global counter that accumulates across
    every magic-link send in the session — without resetting it per-test, a
    test that sends 50+ links earlier in the run would push the global cap
    and starve later tests. We re-seed it to a fresh window after truncate.
    """
    from backend import db
    with db.get_conn() as conn:
        conn.execute("TRUNCATE templates, transactions, sessions, users RESTART IDENTITY CASCADE")
        # Reset global magic-link counter so prior tests don't poison this one.
        conn.execute(
            "UPDATE auth_rate_limit SET send_count = 0, window_start = NOW() "
            "WHERE id = 'global_magic_link'"
        )
    yield


@pytest.fixture
def authed_client(clean_db):
    """A TestClient with a valid session cookie for a fresh user. Most app
    tests want this — they don't care about the auth flow itself, just that
    they're talking to the API as some authenticated user."""
    from fastapi.testclient import TestClient
    from backend import auth, db
    from backend.main import app

    client = TestClient(app)
    with db.get_conn() as conn:
        user_id, plaintext = auth._force_create_session(conn, "alice@example.com")
    client.cookies.set(auth.SESSION_COOKIE, plaintext)
    client.user_id = user_id  # tests can read this for user-scoped assertions
    client.user_email = "alice@example.com"
    yield client


@pytest.fixture
def two_authed_clients(clean_db):
    """Two independent authed clients (alice, bob) for cross-user privacy tests."""
    from fastapi.testclient import TestClient
    from backend import auth, db
    from backend.main import app

    alice = TestClient(app)
    bob = TestClient(app)
    with db.get_conn() as conn:
        alice_id, alice_token = auth._force_create_session(conn, "alice@example.com")
        bob_id, bob_token = auth._force_create_session(conn, "bob@example.com")
    alice.cookies.set(auth.SESSION_COOKIE, alice_token)
    bob.cookies.set(auth.SESSION_COOKIE, bob_token)
    alice.user_id = alice_id
    bob.user_id = bob_id
    yield alice, bob


@pytest.fixture
def isolated_template_dirs(tmp_path, monkeypatch):
    """Redirect uploaded-PDF + mapping-JSON dirs to a per-test tmp_path so
    tests don't pollute the real templates/ tree (and don't trip on each
    other's stale fixture state)."""
    from backend import templates as templates_mod
    test_pdf_dir = tmp_path / "pdf"
    test_mapping_dir = tmp_path / "mappings"
    test_pdf_dir.mkdir()
    test_mapping_dir.mkdir()
    monkeypatch.setattr(templates_mod, "TEMPLATES_PDF_DIR", test_pdf_dir)
    monkeypatch.setattr(templates_mod, "MAPPINGS_DIR", test_mapping_dir)
    yield test_pdf_dir, test_mapping_dir
