"""Enrichment cascade: find people at a company, cheapest provider first.

The design stopped the waterfall at the first provider that returned anything.
That pays for one provider per company but often ends with one contact, or with
contacts that do not verify. Here the stopping rule is a number of *verified*
contacts (`target`): a more expensive provider is called only while the cheaper
ones have not produced that many. `target=1` reproduces the design's behaviour,
and the README compares the two on the same world.

A run claims a batch with SKIP LOCKED (006) and commits the claim before any HTTP
call, so two overlapping runs work on disjoint companies and never pay twice.
Everything found for one company is written in one transaction together with the
company's new status and its ledger, so a crash leaves either all of it or none,
and the reaper (007) puts the claim back.
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
import psycopg

from leadengine.normalize import is_role_email, normalize_email
from leadengine.providers.clients import (
    ApolloClient,
    Contact,
    FindymailClient,
    HunterClient,
    PhantomBusterClient,
    SnovClient,
    _Client,
    usable_email,
)
from leadengine.providers.http import Ledger, ProviderError, RetryPolicy
from leadengine.providers.mock import MockProviders

MOCK_BASE = "http://providers.mock"


@dataclass
class Clients:
    providers: list[_Client]  # cheapest first
    verifier: FindymailClient

    @property
    def phantom_empty_runs(self) -> int:
        return sum(getattr(p, "empty_runs", 0) for p in self.providers)


def mock_clients(
    mock: MockProviders,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    policy: RetryPolicy = RetryPolicy(),
) -> Clients:
    """Real clients wired to the in-process mock. Nothing leaves the process."""
    http = httpx.Client(transport=mock.transport(), timeout=10.0)
    kw = {"policy": policy, "sleep": sleep}
    return Clients(
        providers=[
            ApolloClient(http, f"{MOCK_BASE}/apollo", "mock-key", **kw),
            HunterClient(http, f"{MOCK_BASE}/hunter", "mock-key", **kw),
            SnovClient(http, f"{MOCK_BASE}/snov", "mock-id", client_secret="mock-secret", clock=clock, **kw),
            PhantomBusterClient(http, f"{MOCK_BASE}/phantombuster", "mock-key", **kw),
        ],
        verifier=FindymailClient(http, f"{MOCK_BASE}/findymail", "mock-key", **kw),
    )


@dataclass
class CycleStats:
    claimed: int = 0
    enriched: int = 0
    excluded: int = 0  # nobody usable found anywhere
    retry: int = 0     # a provider failed; back in the queue
    failed: int = 0    # out of attempts; in dead_letter
    released_stale: int = 0
    leads: int = 0
    verify: Counter = field(default_factory=Counter)     # verified / risky / invalid
    skipped: Counter = field(default_factory=Counter)    # role / no_email / low_confidence / duplicate
    kept_by: Counter = field(default_factory=Counter)    # provider -> contacts kept
    called: Counter = field(default_factory=Counter)     # provider -> companies it was called for
    cost_usd: float = 0.0
    calls: int = 0

    def add(self, other: CycleStats) -> None:
        for name in ("claimed", "enriched", "excluded", "retry", "failed", "released_stale", "leads", "calls"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        for name in ("verify", "skipped", "kept_by", "called"):
            getattr(self, name).update(getattr(other, name))
        self.cost_usd = round(self.cost_usd + other.cost_usd, 4)


def enrich_company(domain: str, clients: Clients, ledger: Ledger, stats: CycleStats, *,
                   target: int = 3, min_confidence: int = 80) -> list[Contact]:
    """Run the cascade for one company. Returns every contact kept, with its verification."""
    kept: dict[str, Contact] = {}
    names: set[str] = set()
    verified = 0
    for provider in clients.providers:
        if verified >= target:
            break
        stats.called[provider.name] += 1
        for c in provider.find(domain, names | set(kept), ledger):
            email = usable_email(c)
            if email is None:
                wellformed = normalize_email(c.email)
                role = c.role or (wellformed is not None and is_role_email(wellformed))
                stats.skipped["role" if role else "no_email"] += 1
                continue
            if c.confidence is not None and c.confidence < min_confidence:
                stats.skipped["low_confidence"] += 1
                continue
            if email in kept:
                stats.skipped["duplicate"] += 1
                continue
            c.email = email
            c.verify_result = clients.verifier.verify(email, ledger)
            c.cost_usd = round(c.cost_usd + ledger.records[-1].cost_usd, 4)
            kept[email] = c
            if c.name_key:
                names.add(c.name_key)
            stats.kept_by[provider.name] += 1
            stats.verify[c.verify_result] += 1
            if c.verify_result == "verified":
                verified += 1
    return list(kept.values())


def _write_ledger(conn: psycopg.Connection, run_id: str, company_id, ledger: Ledger) -> None:
    if not ledger.records:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO provider_calls (run_id, company_id, provider, endpoint, http_status, cost_usd, results)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            [(run_id, company_id, r.provider, r.endpoint, r.http_status, r.cost_usd, r.results)
             for r in ledger.records],
        )


def _save_contacts(conn: psycopg.Connection, company_id, contacts: list[Contact]) -> int:
    inserted = 0
    for c in contacts:
        full = " ".join(x for x in (c.first_name, c.last_name) if x) or None
        cur = conn.execute(
            """
            INSERT INTO leads (company_id, first_name, last_name, full_name, title, seniority, email,
                               email_verified, email_verify_source, enrichment_source, enrichment_cost_usd, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'enriched')
            ON CONFLICT (company_id, email) WHERE email IS NOT NULL DO NOTHING
            """,
            (company_id, c.first_name, c.last_name, full, c.title, c.seniority, c.email,
             c.verify_result == "verified", f"findymail:{c.verify_result}", c.source, c.cost_usd),
        )
        inserted += cur.rowcount
    return inserted


def run_cycle(
    conn: psycopg.Connection,
    clients: Clients,
    *,
    batch: int = 25,
    target: int = 3,
    min_confidence: int = 80,
    max_attempts: int = 3,
    stale_after: str = "15 minutes",
    run_id: str | None = None,
) -> CycleStats:
    """Claim up to `batch` companies and enrich them. Needs an idle connection."""
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError("run_cycle() needs an idle connection; commit or roll back first")
    run_id = run_id or uuid.uuid4().hex[:12]
    stats = CycleStats()

    with conn.transaction():
        stats.released_stale = conn.execute(
            "SELECT release_stale_company_claims(%s::interval, %s)", (stale_after, max_attempts)
        ).fetchone()[0]
    with conn.transaction():
        claimed = conn.execute("SELECT id, domain FROM claim_companies(%s)", (batch,)).fetchall()
    stats.claimed = len(claimed)

    for company_id, domain in claimed:
        ledger = Ledger()
        # Per-company counters, merged only if the company is saved: a failed attempt
        # stores nothing, so its contacts must not show up in the totals. Its calls
        # and cost do count, because that money was spent.
        attempt = CycleStats()
        try:
            contacts = enrich_company(domain, clients, ledger, attempt, target=target, min_confidence=min_confidence)
        except ProviderError as e:
            with conn.transaction():
                dead = conn.execute(
                    "SELECT mark_company_failed(%s, 'enrich', %s, %s)", (company_id, str(e), max_attempts)
                ).fetchone()[0]
                _write_ledger(conn, run_id, company_id, ledger)
            stats.called.update(attempt.called)
            stats.failed += int(dead)
            stats.retry += int(not dead)
        except Exception:
            # A bug, not a provider outage: keep the record of what was paid, leave
            # the claim for the reaper, and let the error surface.
            with conn.transaction():
                _write_ledger(conn, run_id, company_id, ledger)
            raise
        else:
            with conn.transaction():
                n = _save_contacts(conn, company_id, contacts)
                conn.execute(
                    """UPDATE companies
                          SET status = %s, enriched_at = now(), last_error = %s
                        WHERE id = %s AND status = 'enriching'""",
                    ("enriched" if n else "excluded", None if n else "no usable contacts from any provider",
                     company_id),
                )
                _write_ledger(conn, run_id, company_id, ledger)
            stats.add(attempt)
            stats.leads += n
            stats.enriched += int(n > 0)
            stats.excluded += int(n == 0)
        stats.calls += len(ledger.records)
        stats.cost_usd = round(stats.cost_usd + ledger.cost_usd, 4)
    return stats


def run_until_done(conn: psycopg.Connection, clients: Clients, *, max_cycles: int = 200, **kw) -> CycleStats:
    """Repeat cycles until nothing is claimable. Bounded by max_cycles."""
    total = CycleStats()
    for _ in range(max_cycles):
        s = run_cycle(conn, clients, **kw)
        total.add(s)
        if s.claimed == 0:
            return total
    raise RuntimeError(f"queue not drained after {max_cycles} cycles")
