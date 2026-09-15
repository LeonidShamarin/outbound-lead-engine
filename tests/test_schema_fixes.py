"""The design bugs this schema fixes, each pinned by a test that failed on the original."""

from __future__ import annotations

import psycopg
import pytest

from leadengine import db


def _company(conn: psycopg.Connection, domain: str) -> str:
    return conn.execute("INSERT INTO companies (domain) VALUES (%s) RETURNING id", (domain,)).fetchone()[0]


def _lead(conn: psycopg.Connection, company_id: str, email: str | None, status: str = "new") -> str:
    return conn.execute(
        "INSERT INTO leads (company_id, email, status) VALUES (%s, %s, %s) RETURNING id",
        (company_id, email, status),
    ).fetchone()[0]


def test_concurrent_claims_never_overlap(db_url: str) -> None:
    with psycopg.connect(db_url) as setup:
        db.migrate(setup)
        for i in range(30):
            _company(setup, f"c{i:02d}.example")
        setup.commit()

    # Two open transactions claim at the same time, as two overlapping cron runs would.
    with psycopg.connect(db_url) as a, psycopg.connect(db_url) as b:
        got_a = {r[0] for r in a.execute("SELECT id FROM claim_companies(20)")}
        got_b = {r[0] for r in b.execute("SELECT id FROM claim_companies(20)")}
        a.commit()
        b.commit()
        pending = a.execute("SELECT count(*) FROM companies WHERE status = 'pending_enrichment'").fetchone()[0]

    assert len(got_a) == 20
    assert len(got_b) == 10
    assert got_a.isdisjoint(got_b)
    assert pending == 0


def test_mark_variables_ready_uses_containment(conn: psycopg.Connection) -> None:
    company = _company(conn, "acme.example")
    theory = conn.execute(
        """INSERT INTO theories (name, hypothesis, required_variables)
           VALUES ('funding', 'raised recently', '["funding_round","tech_stack"]') RETURNING id"""
    ).fetchone()[0]
    ready, partial, none = (_lead(conn, company, f"{n}@acme.example", "theory_assigned") for n in ("a", "b", "c"))
    conn.execute("UPDATE leads SET theory_id = %s", (theory,))
    for lead, keys in ((ready, ("funding_round", "tech_stack", "extra")), (partial, ("funding_round",))):
        for k in keys:
            conn.execute("INSERT INTO variables (lead_id, theory_id, key) VALUES (%s, %s, %s)", (lead, theory, k))

    assert conn.execute("SELECT mark_variables_ready()").fetchone()[0] == 1
    statuses = dict(conn.execute("SELECT id, status FROM leads").fetchall())
    assert statuses[ready] == "variables_ready"
    assert statuses[partial] == "theory_assigned"
    assert statuses[none] == "theory_assigned"


def test_theory_with_no_required_variables_is_ready_immediately(conn: psycopg.Connection) -> None:
    company = _company(conn, "plain.example")
    theory = conn.execute("INSERT INTO theories (name, hypothesis) VALUES ('plain', 'generic') RETURNING id").fetchone()[0]
    lead = _lead(conn, company, "x@plain.example", "theory_assigned")
    conn.execute("UPDATE leads SET theory_id = %s WHERE id = %s", (theory, lead))
    assert conn.execute("SELECT mark_variables_ready()").fetchone()[0] == 1


def test_dead_letter_is_written_once(conn: psycopg.Connection) -> None:
    lead = _lead(conn, _company(conn, "flaky.example"), "p@flaky.example", "scoring")
    results = [
        conn.execute("SELECT mark_lead_failed(%s, 'score', 'timeout', 'enriched', 3)", (lead,)).fetchone()[0]
        for _ in range(6)
    ]
    status, attempts = conn.execute("SELECT status, attempts FROM leads WHERE id = %s", (lead,)).fetchone()
    rows = conn.execute("SELECT count(*) FROM dead_letter WHERE entity_id = %s", (lead,)).fetchone()[0]

    assert results == [False, False, True, False, False, False]
    assert (status, attempts, rows) == ("dead_letter", 3, 1)


def test_failed_lead_goes_back_to_the_given_status_not_to_new(conn: psycopg.Connection) -> None:
    lead = _lead(conn, _company(conn, "retry.example"), "p@retry.example", "drafting")
    conn.execute("SELECT mark_lead_failed(%s, 'copy', 'bad json', 'variables_ready', 3)", (lead,))
    assert conn.execute("SELECT status FROM leads WHERE id = %s", (lead,)).fetchone()[0] == "variables_ready"


def test_same_person_cannot_be_stored_twice_per_company(conn: psycopg.Connection) -> None:
    company = _company(conn, "dup.example")
    _lead(conn, company, "anna@dup.example")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _lead(conn, company, "anna@dup.example")


def test_mixed_case_email_is_rejected_by_the_schema(conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _lead(conn, _company(conn, "case.example"), "Anna@Case.example")


def test_unknown_status_is_rejected(conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _lead(conn, _company(conn, "typo.example"), "a@typo.example", "scroed")


def test_suppressed_lead_is_not_sendable(conn: psycopg.Connection) -> None:
    company = _company(conn, "send.example")
    keep = _lead(conn, company, "keep@send.example", "copy_ready")
    drop = _lead(conn, company, "drop@send.example", "copy_ready")
    conn.execute("UPDATE leads SET email_verified = true")
    conn.execute("INSERT INTO suppressions (email, reason) VALUES ('drop@send.example', 'unsubscribe')")
    sendable = {r[0] for r in conn.execute("SELECT id FROM leads_sendable")}
    assert sendable == {keep}
    assert drop not in sendable


def test_gdpr_delete_removes_data_and_suppresses_even_unknown_emails(conn: psycopg.Connection) -> None:
    lead = _lead(conn, _company(conn, "gdpr.example"), "jane@gdpr.example", "sent")
    conn.execute("INSERT INTO events (lead_id, type, external_id) VALUES (%s, 'sent', 'e1')", (lead,))

    found = conn.execute("SELECT gdpr_delete_by_email('  JANE@gdpr.example ', 'dpo@us.example')").fetchone()[0]
    unknown = conn.execute("SELECT gdpr_delete_by_email('nobody@gdpr.example', 'dpo@us.example')").fetchone()[0]

    assert found["found"] and found["leads_deleted"] == 1 and found["events_deleted"] == 1
    assert unknown["found"] is False
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    suppressed = {r[0] for r in conn.execute("SELECT email FROM suppressions")}
    assert suppressed == {"jane@gdpr.example", "nobody@gdpr.example"}


def test_duplicate_webhook_event_is_ignored(conn: psycopg.Connection) -> None:
    lead = _lead(conn, _company(conn, "hook.example"), "h@hook.example", "sent")
    for _ in range(2):
        conn.execute(
            "INSERT INTO events (external_id, lead_id, type) VALUES ('evt-1', %s, 'open') ON CONFLICT (external_id) DO NOTHING",
            (lead,),
        )
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
