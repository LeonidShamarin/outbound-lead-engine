"""Every test gets a fresh, fully migrated database.

Tests run against a real Postgres, never a mock: the fixes this project is about
(SKIP LOCKED claims, JSONB containment, CHECK constraints) only exist in Postgres.
Each test creates its own database from DATABASE_URL and drops it afterwards, so
tests cannot leak state into each other and can run in any order.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import conninfo, sql

from leadengine import db


def _admin_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set; run tests through scripts/run_tests_capped.ps1")
    return url


@pytest.fixture
def db_url() -> Iterator[str]:
    admin_url = _admin_url()
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    info = conninfo.conninfo_to_dict(admin_url)
    info["dbname"] = name
    url = conninfo.make_conninfo(**info)
    try:
        yield url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def conn(db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(db_url) as c:
        db.migrate(c)
        yield c
