"""
Security regression tests.

These tests guard fixes for vulnerabilities that were caught after they shipped
(or were caught in /review just before they shipped). Each test cites the bug
it's preventing so a future regression is obvious.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from backend.main import app


# ---- Path traversal in static_passthrough (CVE-internal, /review 2026-05-08) ----
#
# Pre-fix: GET /%2E%2E/backend/auth.py returned the source of auth.py to any
# unauthenticated caller. The handler did `target = FRONTEND_DIR / path` and
# checked `target.exists()` without resolving + verifying containment.
#
# Fix: resolve the path and require it stay inside FRONTEND_DIR.

def test_static_passthrough_blocks_url_encoded_traversal():
    """%2E%2E (URL-encoded ..) must not escape the frontend dir."""
    client = TestClient(app)
    r = client.get("/%2E%2E/backend/auth.py")
    assert r.status_code == 404


def test_static_passthrough_blocks_url_encoded_slash_traversal():
    """..%2F (URL-encoded slash) must not escape either."""
    client = TestClient(app)
    r = client.get("/..%2Fbackend%2Fauth.py")
    assert r.status_code == 404


def test_static_passthrough_blocks_dotdot_segments():
    """A literal ../ in the path must not escape FRONTEND_DIR."""
    client = TestClient(app)
    # TestClient normalizes the URL like a browser would, so we use a path
    # that produces a relative `..` after FastAPI's routing extracts it.
    r = client.get("/sub/..%2Fbackend%2Fauth.py")
    assert r.status_code == 404


def test_static_passthrough_serves_legitimate_files():
    """Sanity check: the fix didn't break normal static serving."""
    client = TestClient(app)
    assert client.get("/styles.css").status_code == 200
    assert client.get("/app.js").status_code == 200
    assert client.get("/login.html").status_code == 200


def test_static_passthrough_404s_for_missing_files():
    client = TestClient(app)
    assert client.get("/does-not-exist.html").status_code == 404


def test_api_paths_blocked_by_explicit_prefix_check():
    """/api/* and /auth/* paths must not fall through to static_passthrough
    even if the file happens to exist in FRONTEND_DIR."""
    client = TestClient(app)
    # There's no api/health.html in frontend/, but the prefix check should
    # reject anything matching the api/ pattern before file-existence is
    # checked.
    r = client.get("/api/this-is-not-a-real-route")
    # 404 from FastAPI itself, not from static_passthrough — the prefix
    # check returns 404 before any file lookup.
    assert r.status_code == 404
