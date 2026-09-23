"""The cascade end to end against a real Postgres and the mock providers."""

from __future__ import annotations

import threading

import psycopg
from helpers import person, setup, world

from leadengine import db
from leadengine.enrich import run_cycle, run_until_done
from leadengine.providers.mock import Faults
from leadengine.seed.load import load_intake


def _seed(conn: psycopg.Connection, w: dict) -> None:
    load_intake(conn, w)
    conn.commit()


def _calls(conn, domain: str) -> dict[str, int]:
    return dict(conn.execute(
        """SELECT pc.provider, count(*) FROM provider_calls pc JOIN companies c ON c.id = pc.company_id
            WHERE c.domain = %s GROUP BY pc.provider""", (domain,)).fetchall())


def test_cheap_provider_covers_the_company_so_the_expensive_one_is_never_called(conn) -> None:
    d = "covered.example"
    w = world(*(person(d, f"P{i}", "Novak", apollo=True, phantombuster=True) for i in range(3)))
    _seed(conn, w)
    mock, clients, _, _ = setup(w)
    s = run_cycle(conn, clients, target=3)

    assert s.enriched == 1 and s.leads == 3
    assert mock.count("phantombuster/") == 0 and mock.count("hunter/") == 0
    assert set(_calls(conn, d)) == {"apollo", "findymail"}


def test_expensive_provider_is_called_only_when_cheap_ones_fall_short(conn) -> None:
    d = "hidden.example"
    w = world(person(d, "Anna", "Keller", apollo=True, phantombuster=True),
              person(d, "Olena", "Bondar", phantombuster=True),
              person(d, "Tom", "Hart", phantombuster=True))
    _seed(conn, w)
    mock, clients, _, _ = setup(w)
    s = run_cycle(conn, clients, target=3)

    assert s.called == {"apollo": 1, "hunter": 1, "snov": 1, "phantombuster": 1}
    assert s.kept_by == {"apollo": 1, "phantombuster": 2}
    assert s.skipped["duplicate"] == 1  # Anna came back from PhantomBuster too, stored once
    assert conn.execute("SELECT count(*) FROM leads").fetchone()[0] == 3


def test_target_one_stops_at_the_first_verified_contact_like_the_design(conn) -> None:
    d = "early.example"
    w = world(person(d, "A", "One", apollo=True), person(d, "B", "Two", hunter=True))
    _seed(conn, w)
    mock, clients, _, _ = setup(w)
    run_cycle(conn, clients, target=1)
    assert mock.count("hunter/") == 0


def test_risky_and_invalid_are_stored_but_never_sendable(conn) -> None:
    d = "mixed.example"
    w = world(person(d, "Good", "One", apollo=True), person(d, "Catch", "All", status="risky", apollo=True),
              person(d, "Dead", "Box", status="invalid", apollo=True))
    _seed(conn, w)
    _, clients, _, _ = setup(w)
    run_cycle(conn, clients, target=5)

    rows = dict(conn.execute("SELECT email_verify_source, email_verified FROM leads").fetchall())
    assert rows == {"findymail:verified": True, "findymail:risky": False, "findymail:invalid": False}
    conn.execute("UPDATE leads SET status = 'copy_ready'")
    assert conn.execute("SELECT count(*) FROM leads_sendable").fetchone()[0] == 1


def test_role_mailboxes_and_low_confidence_are_not_paid_for(conn) -> None:
    d = "inbox.example"
    w = world(person(d, None, None, email=f"info@{d}", role=True, hunter=True),
              person(d, "Weak", "Guess", status="invalid", hunter=True),
              person(d, "Real", "Person", hunter=True))
    _seed(conn, w)
    mock, clients, _, _ = setup(w)
    s = run_cycle(conn, clients, target=1, min_confidence=80)

    assert s.skipped["role"] == 1 and s.skipped["low_confidence"] == 1
    assert mock.count("findymail/") == 1
    assert conn.execute("SELECT email FROM leads").fetchall() == [(f"real.person@{d}",)]


def test_rate_limits_during_the_cascade_do_not_change_the_result(conn, db_url) -> None:
    people = [person(f"c{i}.example", f"P{j}", "Novak", apollo=j == 0, hunter=j == 1, snov=j == 2)
              for i in range(5) for j in range(3)]
    w = world(*people)

    _seed(conn, w)
    _, clean, _, _ = setup(w)
    run_until_done(conn, clean, target=3)
    expected = conn.execute("SELECT email FROM leads ORDER BY email").fetchall()
    conn.commit()  # release the read lock, or TRUNCATE below waits on it forever

    with psycopg.connect(db_url) as c2:
        c2.execute("TRUNCATE companies CASCADE")
        c2.commit()
        _seed(c2, w)
        _, noisy, slept, _ = setup(w, Faults(rate_429=0.3, retry_after_s=1), max_attempts=10)
        s = run_until_done(c2, noisy, target=3)
        got = c2.execute("SELECT email FROM leads ORDER BY email").fetchall()

    assert got == expected and len(got) == 15
    assert s.failed == 0 and slept  # some calls were really rate limited and waited


def test_provider_outage_returns_company_to_queue_then_dead_letters_once(conn) -> None:
    d = "down.example"
    w = world(person(d, "A", "B", hunter=True), person(d, "C", "D", apollo=True))
    _seed(conn, w)
    faults = Faults(script={"hunter/": [503] * 100})
    _, clients, _, _ = setup(w, faults, max_attempts=2)

    first = run_cycle(conn, clients, max_attempts=3)
    status, attempts = conn.execute("SELECT status, attempts FROM companies").fetchone()
    assert (first.retry, status, attempts) == (1, "pending_enrichment", 1)
    # Apollo found C before Hunter failed; nothing was saved, so nothing is counted as kept.
    assert first.called["apollo"] == 1 and not first.kept_by and not first.verify
    assert conn.execute("SELECT count(*) FROM leads").fetchone()[0] == 0
    # The failed calls are in the ledger even though nothing was saved.
    assert _calls(conn, d)["hunter"] == 2
    conn.commit()

    total = run_until_done(conn, clients, max_attempts=3)
    assert total.failed == 1
    assert conn.execute("SELECT status FROM companies").fetchone()[0] == "failed"
    assert conn.execute("SELECT count(*) FROM dead_letter WHERE entity_type = 'company'").fetchone()[0] == 1
    conn.commit()
    assert run_cycle(conn, clients).claimed == 0


def test_two_parallel_runs_never_pay_twice_for_a_company(db_url) -> None:
    w = world(*(person(f"p{i:02d}.example", "A", "B", hunter=True, phantombuster=True) for i in range(40)))
    with psycopg.connect(db_url) as setup_conn:
        db.migrate(setup_conn)
        _seed(setup_conn, w)
    _, clients, _, _ = setup(w)  # one shared mock, like one real provider account

    errors: list[BaseException] = []
    start = threading.Barrier(2, timeout=30)

    def worker(run_id: str) -> None:
        try:
            with psycopg.connect(db_url) as c:
                start.wait()  # both runs begin claiming at the same moment
                run_until_done(c, clients, batch=7, target=2, run_id=run_id)
        except BaseException as e:  # surfaced below; a thread must not swallow it
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(f"run{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors and not any(t.is_alive() for t in threads)

    with psycopg.connect(db_url) as c:
        runs_per_company = c.execute(
            "SELECT company_id, count(DISTINCT run_id) FROM provider_calls GROUP BY company_id").fetchall()
        paid_searches = c.execute(
            "SELECT count(*) FROM provider_calls WHERE provider = 'hunter' AND endpoint = 'domain_search'").fetchone()[0]
        runs = {r[0] for r in c.execute("SELECT DISTINCT run_id FROM provider_calls")}
    assert len(runs_per_company) == 40
    assert all(n == 1 for _, n in runs_per_company)
    assert paid_searches == 40
    assert runs == {"run0", "run1"}  # both runs really worked


def test_stale_claim_is_released_and_counts_as_an_attempt(conn) -> None:
    w = world(person("stuck.example", "A", "B", apollo=True))
    _seed(conn, w)
    conn.execute("SELECT claim_companies(10)")  # a run that claimed and then died
    conn.execute("UPDATE companies SET last_attempt_at = now() - interval '1 hour'")
    conn.commit()

    _, clients, _, _ = setup(w)
    s = run_cycle(conn, clients, stale_after="15 minutes")
    assert s.released_stale == 1 and s.enriched == 1
    assert conn.execute("SELECT attempts, status FROM companies").fetchone() == (1, "enriched")


def test_second_run_after_everything_is_enriched_does_nothing(conn) -> None:
    w = world(person("done.example", "A", "B", apollo=True))
    _seed(conn, w)
    mock, clients, _, _ = setup(w)
    run_until_done(conn, clients)
    before = len(mock.calls)
    assert run_cycle(conn, clients).claimed == 0
    assert len(mock.calls) == before


def test_company_with_nobody_usable_is_excluded_not_retried(conn) -> None:
    w = world(person("ghost.example", None, None, email="info@ghost.example", role=True, hunter=True))
    _seed(conn, w)
    _, clients, _, _ = setup(w)
    s = run_until_done(conn, clients)
    assert (s.excluded, s.retry) == (1, 0)
    assert conn.execute("SELECT status, last_error FROM companies").fetchone() == (
        "excluded", "no usable contacts from any provider")


def test_lead_cost_includes_reveal_and_verification(conn) -> None:
    w = world(person("cost.example", "A", "B", apollo=True))
    _seed(conn, w)
    _, clients, _, _ = setup(w)
    run_cycle(conn, clients, target=1)
    lead_cost = conn.execute("SELECT enrichment_cost_usd FROM leads").fetchone()[0]
    ledger_cost = conn.execute("SELECT sum(cost_usd) FROM provider_calls").fetchone()[0]
    assert float(lead_cost) == 0.028  # 0.016 Apollo reveal + 0.012 Findymail
    assert ledger_cost == lead_cost  # Apollo search was free, target reached, nothing else called


def test_company_level_calls_are_in_the_ledger_not_on_the_lead(conn) -> None:
    # One person, target 3: the cascade goes on through Hunter and Snov, finds
    # nobody new, and those per-domain fees belong to the company, not the lead.
    w = world(person("thin.example", "A", "B", apollo=True))
    _seed(conn, w)
    _, clients, _, _ = setup(w)
    run_cycle(conn, clients, target=3)
    lead_cost = conn.execute("SELECT enrichment_cost_usd FROM leads").fetchone()[0]
    by_provider = dict(conn.execute(
        "SELECT provider, sum(cost_usd)::float FROM provider_calls GROUP BY provider").fetchall())
    assert float(lead_cost) == 0.028
    assert by_provider == {"apollo": 0.016, "findymail": 0.012, "hunter": 0.01, "snov": 0.012,
                           "phantombuster": 0.0}
