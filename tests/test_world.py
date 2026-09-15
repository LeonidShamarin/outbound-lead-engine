from __future__ import annotations

import psycopg
import pytest

from leadengine.normalize import normalize_domain
from leadengine.seed.load import load_intake
from leadengine.seed.world import PROVIDERS, build_world


def test_same_seed_gives_the_same_world() -> None:
    assert build_world(seed=7, n_companies=40) == build_world(seed=7, n_companies=40)
    assert build_world(seed=7, n_companies=40) != build_world(seed=8, n_companies=40)


def test_every_generated_address_is_on_the_reserved_tld() -> None:
    world = build_world(seed=1, n_companies=120)
    assert all(c["domain"].endswith(".example") for c in world["companies"])
    assert all(p["email"].endswith(".example") for p in world["people"])


def test_world_contains_the_dirt_the_pipeline_must_handle() -> None:
    world = build_world(seed=42, n_companies=250)
    people = world["people"]
    normalised = [normalize_domain(r) for r in world["intake"]]

    assert normalised.count(None) >= 1, "intake has rows that are not domains"
    assert len([d for d in normalised if d]) > len(world["companies"]), "intake repeats companies"
    assert any(p["role_email"] for p in people)
    assert any(p["last_name"] is None for p in people)
    assert {p["email_status"] for p in people} == {"valid", "risky", "invalid"}
    only_expensive = [p for p in people if p["providers"]["phantombuster"] and not any(p["providers"][x] for x in PROVIDERS[:3])]
    found_by_none = [p for p in people if not any(p["providers"].values())]
    assert only_expensive, "some people exist only in the most expensive provider"
    assert found_by_none, "some people cannot be found at all"


def test_unsatisfiable_size_stops_instead_of_looping() -> None:
    with pytest.raises(ValueError):
        build_world(seed=1, n_companies=5001)


def test_intake_load_collapses_duplicates_and_is_idempotent(conn: psycopg.Connection) -> None:
    world = build_world(seed=42, n_companies=60)
    first = load_intake(conn, world)
    second = load_intake(conn, world)
    stored = conn.execute("SELECT count(*) FROM companies").fetchone()[0]

    assert first.inserted == len(world["companies"]) == stored
    assert first.rows == first.inserted + first.duplicates + first.rejected
    assert first.rejected >= 1
    assert second.inserted == 0
