"""Load the intake list into `companies`, collapsing duplicates by normalised domain."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from leadengine.normalize import normalize_domain


@dataclass(frozen=True)
class IntakeResult:
    rows: int
    inserted: int
    duplicates: int
    rejected: int


def load_intake(conn: psycopg.Connection, world: dict, source: str = "synthetic") -> IntakeResult:
    """Insert companies from world['intake']. Idempotent: a second run inserts nothing.

    Company attributes come from the world by domain; a raw row that normalises to a
    domain the world does not know is kept with attributes empty, as a real list would.
    """
    by_domain = {c["domain"]: c for c in world["companies"]}
    inserted = duplicates = rejected = 0
    seen: set[str] = set()

    with conn.transaction():
        for raw in world["intake"]:
            domain = normalize_domain(raw)
            if domain is None:
                rejected += 1
                continue
            if domain in seen:
                duplicates += 1
                continue
            seen.add(domain)
            c = by_domain.get(domain, {})
            cur = conn.execute(
                """
                INSERT INTO companies (domain, name, industry, size_band, country, source)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (domain) DO NOTHING
                """,
                (domain, c.get("name"), c.get("industry"), c.get("size_band"), c.get("country"), source),
            )
            if cur.rowcount == 1:
                inserted += 1
            else:
                duplicates += 1

    return IntakeResult(rows=len(world["intake"]), inserted=inserted, duplicates=duplicates, rejected=rejected)
