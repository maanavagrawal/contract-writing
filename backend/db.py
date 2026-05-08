"""
sqlite connection management + a tiny migration runner.

We use raw sqlite3 (no ORM): the data shapes are simple, the queries are few,
and Pydantic owns row→object validation. Migrations are .sql files in
backend/migrations/, plus optional Python scripts named `<num>_*.py` that
expose a `run(conn)` function for seed data.

  - WAL journaling for better read concurrency under FastAPI.
  - Foreign keys enabled (sqlite has them off by default — common footgun).
  - _migrations table tracks applied migration ids so we don't re-run them.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "app.sqlite"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _connect(path: Path) -> sqlite3.Connection:
    """Open a connection with sane defaults baked in."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit; we use BEGIN explicitly
    conn.row_factory = sqlite3.Row
    # PRAGMAs that aren't persistent across connections. Set them every time.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def get_conn(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Short-lived connection. Open per-request rather than holding a global —
    sqlite3 connections aren't thread-safe in default mode and FastAPI runs
    requests on a thread pool."""
    conn = _connect(path or DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            id          TEXT PRIMARY KEY,
            applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    """)


def _applied_ids(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT id FROM _migrations")}


def _list_migrations() -> list[Path]:
    """Return migration files sorted by their numeric prefix.
    Both .sql and .py count; .py files run a `run(conn)` function."""
    if not MIGRATIONS_DIR.exists():
        return []
    files = [p for p in MIGRATIONS_DIR.iterdir() if p.suffix in (".sql", ".py") and not p.name.startswith("_")]
    files.sort(key=lambda p: p.name)
    return files


def _apply_sql(conn: sqlite3.Connection, path: Path) -> None:
    conn.executescript(path.read_text())


def _apply_py(conn: sqlite3.Connection, path: Path) -> None:
    """Load a migration module by path and call its run(conn). Used for seed
    data that's easier to express in Python than in SQL (reading existing
    mapping JSONs, generating UUIDs, etc.)."""
    spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load migration: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "run"):
        raise RuntimeError(f"migration {path.name} missing run(conn)")
    module.run(conn)


def run_migrations(path: Path | None = None) -> list[str]:
    """Apply any unapplied migrations in order. Returns the ids that were
    applied this run (empty list if nothing to do).

    Note on transactions: sqlite3.executescript() implicitly commits any open
    transaction before running, so we can't wrap .sql migrations in BEGIN/COMMIT
    here. .py migrations get an explicit transaction so seed inserts roll back
    on error. Either way, _migrations is updated last and only on success.
    """
    applied_now: list[str] = []
    with get_conn(path) as conn:
        _ensure_migrations_table(conn)
        already = _applied_ids(conn)
        for mig in _list_migrations():
            mig_id = mig.stem
            if mig_id in already:
                continue
            if mig.suffix == ".sql":
                _apply_sql(conn, mig)
                conn.execute("INSERT INTO _migrations (id) VALUES (?)", (mig_id,))
            else:
                conn.execute("BEGIN")
                try:
                    _apply_py(conn, mig)
                    conn.execute("INSERT INTO _migrations (id) VALUES (?)", (mig_id,))
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            applied_now.append(mig_id)
    return applied_now
