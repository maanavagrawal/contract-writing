"""
Postgres connection management + a tiny migration runner.

Backed by psycopg 3 + ConnectionPool. We keep the same per-request
context-manager shape the rest of the app already uses (`with get_conn() as
conn: ...`) so existing call sites don't change.

Migrations are .sql files in backend/migrations/, plus optional Python scripts
named `<num>_*.py` that expose a `run(conn)` function. The numeric prefix
defines apply order. _migrations table tracks applied ids.
"""
from __future__ import annotations

import importlib.util
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg_pool import ConnectionPool

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Default DSN points at a local Postgres for dev. Production (Railway) sets
# DATABASE_URL to the managed Postgres add-on. Tests override via the
# TEST_DATABASE_URL env var (set by the testcontainers fixture).
_DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/postgres"

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _resolve_dsn() -> str:
    """Test runs override via TEST_DATABASE_URL so a misconfigured local env
    can't accidentally hit production. DATABASE_URL is the production var."""
    return os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or _DEFAULT_DSN


def _get_pool() -> ConnectionPool:
    """Lazy-init a process-wide connection pool. Sized for FastAPI's default
    thread pool (40) plus headroom; min_size=1 keeps a connection warm so the
    first request after idle isn't slow."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    conninfo=_resolve_dsn(),
                    min_size=1,
                    max_size=10,
                    kwargs={"autocommit": True},
                    open=True,
                )
    return _pool


def reset_pool_for_tests() -> None:
    """Tests rebind DATABASE_URL after the fixture spins Postgres up. The
    pool, if already created against a stale DSN, has to be torn down so the
    next get_conn() rebuilds against the new DSN."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.close()
            except Exception:
                pass
            _pool = None


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    """Borrow a connection from the pool for the duration of the `with` block.
    Autocommit is on at the pool level; explicit transactions go through
    `conn.transaction()` when callers need atomicity."""
    pool = _get_pool()
    with pool.connection() as conn:
        yield conn


def _ensure_migrations_table(conn: psycopg.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            id          TEXT PRIMARY KEY,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


def _applied_ids(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute("SELECT id FROM _migrations").fetchall()
    return {r[0] for r in rows}


def _list_migrations() -> list[Path]:
    """Return migration files sorted by their numeric prefix.
    Both .sql and .py count; .py files run a `run(conn)` function."""
    if not MIGRATIONS_DIR.exists():
        return []
    files = [p for p in MIGRATIONS_DIR.iterdir() if p.suffix in (".sql", ".py") and not p.name.startswith("_")]
    files.sort(key=lambda p: p.name)
    return files


def _apply_sql(conn: psycopg.Connection, path: Path) -> None:
    """psycopg can execute multi-statement SQL in one execute() call. We wrap
    in a transaction so a failure mid-script rolls back instead of leaving the
    DB half-migrated."""
    sql = path.read_text()
    with conn.transaction():
        conn.execute(sql)


def _apply_py(conn: psycopg.Connection, path: Path) -> None:
    """Load a migration module by path and call its run(conn). Used for seed
    data that's easier to express in Python than in SQL."""
    spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load migration: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "run"):
        raise RuntimeError(f"migration {path.name} missing run(conn)")
    with conn.transaction():
        module.run(conn)


def run_migrations() -> list[str]:
    """Apply any unapplied migrations in order. Returns the ids that were
    applied this run (empty list if nothing to do).

    Idempotent: subsequent runs against the same DB are no-ops because every
    applied migration's id is recorded in the _migrations table.
    """
    applied_now: list[str] = []
    with get_conn() as conn:
        _ensure_migrations_table(conn)
        already = _applied_ids(conn)
        for mig in _list_migrations():
            mig_id = mig.stem
            if mig_id in already:
                continue
            if mig.suffix == ".sql":
                _apply_sql(conn, mig)
            else:
                _apply_py(conn, mig)
            conn.execute("INSERT INTO _migrations (id) VALUES (%s)", (mig_id,))
            applied_now.append(mig_id)
    return applied_now
