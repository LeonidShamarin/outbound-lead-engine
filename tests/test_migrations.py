from __future__ import annotations

import psycopg
import pytest

from leadengine import db


def test_all_migrations_apply_and_second_run_is_a_noop(db_url: str) -> None:
    with psycopg.connect(db_url) as conn:
        first = db.migrate(conn)
        second = db.migrate(conn)
    assert first == [p.name for p in db.migration_files()]
    assert second == []


def test_edited_migration_is_refused(db_url: str, tmp_path) -> None:
    for p in db.migration_files():
        (tmp_path / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    with psycopg.connect(db_url) as conn:
        db.migrate(conn, tmp_path)
        first = sorted(tmp_path.glob("*.sql"))[0]
        first.write_text(first.read_text(encoding="utf-8") + "\n-- edited\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="edited after being applied"):
            db.migrate(conn, tmp_path)


def test_failing_migration_leaves_nothing_behind(db_url: str, tmp_path) -> None:
    (tmp_path / "001_ok.sql").write_text("CREATE TABLE ok_table (id int);", encoding="utf-8")
    (tmp_path / "002_bad.sql").write_text("CREATE TABLE half (id int); SELECT no_such_column FROM half;", encoding="utf-8")
    with psycopg.connect(db_url) as conn:
        with pytest.raises(psycopg.errors.UndefinedColumn):
            db.migrate(conn, tmp_path)
        conn.rollback()
        names = [r[0] for r in conn.execute("SELECT name FROM schema_migrations ORDER BY name")]
        half = conn.execute("SELECT to_regclass('public.half')").fetchone()[0]
    assert names == ["001_ok.sql"]
    assert half is None


def test_migrate_refuses_a_connection_with_an_open_transaction(db_url: str) -> None:
    with psycopg.connect(db_url) as conn:
        conn.execute("SELECT 1")
        with pytest.raises(RuntimeError, match="idle connection"):
            db.migrate(conn)


def test_migration_names_must_be_numbered(tmp_path) -> None:
    (tmp_path / "schema.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(RuntimeError, match="three-digit"):
        db.migration_files(tmp_path)
