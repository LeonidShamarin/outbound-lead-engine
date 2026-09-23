"""Company signals: the "why now" facts a theory and an email are built on.

The design fetched them from Crunchbase (funding), Apify (hiring, news) and
BuiltWith (tech stack), plus a LinkedIn activity phantom that this project does
not automate. Here a deterministic mock stands in: the same domain always has the
same signals, most companies have some, and some have none, which is the case the
design never handled.

All dates are relative to a fixed reference day, so a run today and a run next
month produce the same words and the eval stays comparable.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

import psycopg

REFERENCE_DAY = "2026-09-01"
MONTHS = ("March", "April", "May", "June", "July", "August")

# Per-lookup prices from the design's cost table; BuiltWith had no line, $0.05 assumed.
SOURCES = {
    "funding_round": ("crunchbase", 0.05),
    "hiring_pace": ("apify_jobs", 0.10),
    "tech_stack_change": ("builtwith", 0.05),
    "company_news": ("apify_news", 0.10),
}
TOOLS = ("Segment", "HubSpot", "Mixpanel", "Salesforce", "Amplitude", "Stripe Billing")
CITIES = ("Warsaw", "Austin", "Lisbon", "Toronto", "Amsterdam", "Manchester")


@dataclass(frozen=True)
class Signal:
    key: str
    value: str | None
    source: str
    cost_usd: float


def signals_for(domain: str, seed: int = 0) -> list[Signal]:
    rng = random.Random(int(hashlib.sha1(f"{seed}|{domain}".encode()).hexdigest()[:16], 16))
    month = rng.choice(MONTHS)
    values = {
        "funding_round": (f"Series {rng.choice('ABC')}, ${rng.choice((8, 12, 18, 24, 35, 50))}M, "
                          f"announced in {month} 2026") if rng.random() < 0.35 else None,
        "hiring_pace": (f"{rng.randint(3, 12)} open sales and marketing roles posted in the 30 days "
                        f"before {REFERENCE_DAY}") if rng.random() < 0.5 else None,
        "tech_stack_change": (f"added {rng.choice(TOOLS)} to its stack in {month} 2026"
                              if rng.random() < 0.4 else None),
        "company_news": (f"opened an office in {rng.choice(CITIES)} in {month} 2026"
                         if rng.random() < 0.4 else None),
    }
    return [Signal(k, v, *SOURCES[k]) for k, v in values.items()]


def fetch_company_signals(conn: psycopg.Connection, batch: int = 50, seed: int = 0) -> int:
    """Fetch signals for companies that have scored leads and no signals yet. Returns companies done."""
    with conn.transaction():
        companies = conn.execute(
            """
            SELECT c.id, c.domain FROM companies c
             WHERE EXISTS (SELECT 1 FROM leads l WHERE l.company_id = c.id AND l.status = 'scored')
               AND NOT EXISTS (SELECT 1 FROM company_signals s WHERE s.company_id = c.id)
             ORDER BY c.created_at, c.id
             LIMIT %s
             FOR UPDATE OF c SKIP LOCKED
            """,
            (batch,),
        ).fetchall()
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO company_signals (company_id, key, value, source, cost_usd)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                [(cid, s.key, s.value, s.source, s.cost_usd) for cid, domain in companies
                 for s in signals_for(domain, seed)],
            )
    return len(companies)
