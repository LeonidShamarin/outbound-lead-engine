"""Database access and a minimal forward-only migration runner.

Migrations are plain SQL files in db/migrations, applied in filename order. Each
file runs in its own transaction together with its row in schema_migrations, so
a failing file leaves nothing half-applied and the next run retries it.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"

# A migration runner that silently skips files it cannot parse is worse than none,
# so the file set is bounded and every name must follow NNN_description.sql.
MAX_MIGRATIONS = 500


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    return url


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or database_url())


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    files = sorted(directory.glob("*.sql"))
    if len(files) > MAX_MIGRATIONS:
        raise RuntimeError(f"{len(files)} migration files, more than the {MAX_MIGRATIONS} limit")
    for f in files:
        prefix = f.name.split("_", 1)[0]
        if not (len(prefix) == 3 and prefix.isdigit()):
            raise RuntimeError(f"migration {f.name} does not start with a three-digit number")
    return files


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def migrate(conn: psycopg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations. Returns the names applied in this call.

    An already-applied file whose content changed raises instead of being skipped:
    editing history is how two environments end up with different schemas.
    """
    # In psycopg 3 a transaction() block only commits when no transaction is open
    # yet; inside an open one it is a savepoint. A bare execute() opens one
    # implicitly, so every statement here lives inside a block, and the caller must
    # hand over an idle connection. Otherwise all files would share one outer
    # transaction and a failure in file N would silently undo files 1..N-1.
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError("migrate() needs an idle connection; commit or roll back first")

    with conn.transaction():
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              name       TEXT PRIMARY KEY,
              checksum   TEXT NOT NULL,
              applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        applied = dict(conn.execute("SELECT name, checksum FROM schema_migrations").fetchall())

    done: list[str] = []
    for path in migration_files(directory):
        sql = path.read_text(encoding="utf-8")
        checksum = _checksum(sql)
        if path.name in applied:
            if applied[path.name] != checksum:
                raise RuntimeError(f"migration {path.name} was edited after being applied")
            continue
        with conn.transaction():
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations (name, checksum) VALUES (%s, %s)",
                (path.name, checksum),
            )
        done.append(path.name)
    return done
