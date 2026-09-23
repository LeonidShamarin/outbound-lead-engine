"""Stage 4: signed webhook, sending, kill switch, simulator, theory generator."""

from __future__ import annotations

import json
import time
from datetime import datetime

import pytest

from leadengine.killswitch import DESIGN_RULE, UPPER_RULE, evaluate_theories, simulate_rule, wilson
from leadengine.llm import FakeLLM, LLMInvalid
from leadengine.sending import ensure_mailboxes, queue_for_sending, variant_for
from leadengine.theorygen import TheoryProposals, propose, validate_proposals
from leadengine.webhook import WebhookRejected, apply_event, handle, sign, verify

SECRET = "whsec_test"
KW = lambda text: ("positive" if "interested" in text.lower() else  # noqa: E731
                   "ooo" if "office" in text.lower() else
                   "unsubscribe" if "remove" in text.lower() else "neutral", "test")


# --- signature ---------------------------------------------------------------

def test_valid_signature_passes() -> None:
    body = b'{"id": "e1"}'
    verify(SECRET, sign(SECRET, body), body)


@pytest.mark.parametrize("header,reason", [
    (None, "missing signature"),
    ("garbage", "malformed"),
    ("t=abc,v1=00", "malformed"),
])
def test_bad_headers_are_rejected(header, reason) -> None:
    with pytest.raises(WebhookRejected, match=reason):
        verify(SECRET, header, b"{}")


def test_tampered_body_is_rejected_and_the_error_does_not_leak_the_signature() -> None:
    header = sign(SECRET, b'{"type": "open"}')
    with pytest.raises(WebhookRejected) as e:
        verify(SECRET, header, b'{"type": "reply"}')
    assert e.value.reason == "bad signature"
    # The design's verifier put the expected HMAC in its error message.
    import hashlib
    import hmac
    t = header.split(",")[0][2:]
    expected = hmac.new(SECRET.encode(), f"{t}.".encode() + b'{"type": "reply"}', hashlib.sha256).hexdigest()
    assert expected not in str(e.value)


def test_signature_is_over_raw_bytes_not_reparsed_json() -> None:
    body = b'{"b": 1,   "a": 2}'
    header = sign(SECRET, body)
    verify(SECRET, header, body)  # whitespace and key order are part of what was signed
    with pytest.raises(WebhookRejected):
        verify(SECRET, header, json.dumps(json.loads(body)).encode())


def test_old_requests_cannot_be_replayed() -> None:
    body = b'{"id": "e1"}'
    old = sign(SECRET, body, t=int(time.time()) - 3600)
    with pytest.raises(WebhookRejected, match="stale"):
        verify(SECRET, old, body)


# --- helpers -------------------------------------------------------------------

def _theory(conn, name, status="active"):
    return conn.execute("INSERT INTO theories (name, hypothesis, status) VALUES (%s, 'h', %s) RETURNING id",
                        (name, status)).fetchone()[0]


def _sendable(conn, n, theory, domain="send.example", score=4):
    company = conn.execute("INSERT INTO companies (domain, status) VALUES (%s, 'enriched') "
                           "ON CONFLICT (domain) DO UPDATE SET status = 'enriched' RETURNING id", (domain,)).fetchone()[0]
    ids = []
    for i in range(n):
        lid = conn.execute(
            """INSERT INTO leads (company_id, first_name, email, email_verified, status, theory_id, lead_score)
               VALUES (%s, 'P', %s, true, 'copy_ready', %s, %s) RETURNING id""",
            (company, f"p{i}.{str(theory)[:4]}@{domain}", theory, score)).fetchone()[0]
        for v in "AB":
            conn.execute("INSERT INTO outgoing_messages (lead_id, theory_id, variant, subject, body) "
                         "VALUES (%s, %s, %s, 's', %s)", (lid, theory, v, f"body {v}"))
        ids.append(lid)
    return ids


def _event(etype, email, eid, campaign, **extra):
    return {"id": eid, "type": etype, "lead_email": email, "campaign_id": campaign, "provider": "simulator", **extra}


# --- sending -------------------------------------------------------------------

def test_mailbox_capacity_is_per_day_and_variant_is_stable(conn) -> None:
    t = _theory(conn, "t1")
    _sendable(conn, 25, t)
    conn.commit()
    ensure_mailboxes(conn, n=2, daily_limit=5)
    day1 = queue_for_sending(conn, now=datetime(2026, 9, 1, 9))
    again = queue_for_sending(conn, now=datetime(2026, 9, 1, 15))
    day2 = queue_for_sending(conn, now=datetime(2026, 9, 2, 9))
    assert (len(day1), len(again), len(day2)) == (10, 0, 10)
    assert all(q.variant == variant_for(q.lead_id) and q.body == f"body {q.variant}" for q in day1)
    assert conn.execute("SELECT count(*) FROM campaigns").fetchone()[0] == 1


def test_suppressed_and_paused_theory_leads_are_not_queued(conn) -> None:
    good, paused = _theory(conn, "good"), _theory(conn, "off", "paused")
    keep = _sendable(conn, 1, good)
    _sendable(conn, 2, paused, domain="off.example")
    blocked = _sendable(conn, 1, good, domain="blocked.example")[0]
    email = conn.execute("SELECT email FROM leads WHERE id = %s", (blocked,)).fetchone()[0]
    conn.execute("INSERT INTO suppressions (email, reason) VALUES (%s, 'unsubscribe')", (email,))
    conn.commit()
    ensure_mailboxes(conn)
    assert [q.lead_id for q in queue_for_sending(conn)] == [str(keep[0])]


# --- event ingest --------------------------------------------------------------

def _queued_lead(conn):
    t = _theory(conn, "t1")
    _sendable(conn, 1, t)
    conn.commit()
    ensure_mailboxes(conn)
    q = queue_for_sending(conn)[0]
    return q


def test_event_lifecycle_and_idempotency(conn) -> None:
    q = _queued_lead(conn)
    sent = _event("sent", q.email, "e1", q.campaign_external_id)
    assert apply_event(conn, sent, KW).lead_status == "sent"
    assert apply_event(conn, sent, KW).duplicate
    ooo = apply_event(conn, _event("reply", q.email, "e2", q.campaign_external_id, text="I'm out of office"), KW)
    assert ooo.reply_class == "ooo" and ooo.lead_status is None  # an auto-reply is not a reply
    pos = apply_event(conn, _event("reply", q.email, "e3", q.campaign_external_id, text="Interested!"), KW)
    assert pos.lead_status == "replied"
    assert conn.execute("SELECT sent_at IS NOT NULL FROM outgoing_messages WHERE variant = %s",
                        (q.variant,)).fetchone()[0]


def test_unsubscribe_in_a_reply_suppresses_and_a_later_reply_does_not_undo_it(conn) -> None:
    q = _queued_lead(conn)
    apply_event(conn, _event("sent", q.email, "e1", q.campaign_external_id), KW)
    r = apply_event(conn, _event("reply", q.email, "e2", q.campaign_external_id, text="remove me"), KW)
    assert r.lead_status == "unsubscribed"
    later = apply_event(conn, _event("reply", q.email, "e3", q.campaign_external_id, text="Interested now"), KW)
    assert later.lead_status is None
    assert conn.execute("SELECT status FROM leads").fetchone()[0] == "unsubscribed"
    assert conn.execute("SELECT reason FROM suppressions").fetchone()[0] == "unsubscribe"


def test_bounce_unverifies_and_suppresses(conn) -> None:
    q = _queued_lead(conn)
    apply_event(conn, _event("bounce", q.email, "b1", q.campaign_external_id), KW)
    assert conn.execute("SELECT status, email_verified FROM leads").fetchone() == ("bounced", False)
    assert conn.execute("SELECT reason FROM suppressions").fetchone()[0] == "bounce"


def test_handle_rejects_and_records_without_storing_the_body(conn) -> None:
    q = _queued_lead(conn)
    body = json.dumps(_event("sent", q.email, "e1", q.campaign_external_id)).encode()
    assert handle(conn, SECRET, sign("wrong", body), body, KW)[0] == 401
    assert handle(conn, SECRET, sign(SECRET, b"not json"), b"not json", KW)[0] == 400
    unknown = json.dumps(_event("sent", "nobody@x.example", "e9", "c")).encode()
    assert handle(conn, SECRET, sign(SECRET, unknown), unknown, KW)[0] == 422
    status, out = handle(conn, SECRET, sign(SECRET, body), body, KW)
    assert (status, out) == (200, {"ok": True, "duplicate": False})
    reasons = [r[0] for r in conn.execute("SELECT reason FROM webhook_rejections ORDER BY id")]
    assert reasons == ["bad signature", "body is not JSON", "unknown lead"]
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1


# --- kill switch -----------------------------------------------------------------

def test_wilson_matches_known_values() -> None:
    lo, hi = wilson(3, 300)
    assert lo == pytest.approx(0.0034, abs=1e-4) and hi == pytest.approx(0.0290, abs=1e-4)
    assert wilson(0, 0) == (0.0, 1.0)


def test_design_rule_pauses_a_theory_at_twice_its_threshold_and_ours_does_not() -> None:
    # 3 positives in 300 is exactly what a 1% theory produces on average.
    assert DESIGN_RULE.should_pause(3, 300) is True
    assert UPPER_RULE.should_pause(3, 300) is False
    assert UPPER_RULE.should_pause(0, 400) is True


def test_monte_carlo_is_reproducible_and_bounded() -> None:
    a = simulate_rule(DESIGN_RULE, 0.01, runs=200, max_sent=600)
    assert a == simulate_rule(DESIGN_RULE, 0.01, runs=200, max_sent=600)
    assert simulate_rule(UPPER_RULE, 0.03, runs=200, max_sent=600)["paused_share"] < 0.02


def _with_events(conn, theory, sent, positive, domain):
    ids = _sendable(conn, sent, theory, domain=domain)
    campaign = conn.execute("INSERT INTO campaigns (theory_id, external_campaign_id, provider) "
                            "VALUES (%s, %s, 'simulator') RETURNING id", (theory, f"c-{domain}")).fetchone()[0]
    for i, lid in enumerate(ids):
        conn.execute("UPDATE leads SET status = 'sent' WHERE id = %s", (lid,))
        conn.execute("INSERT INTO events (external_id, lead_id, campaign_id, type) VALUES (%s, %s, %s, 'sent')",
                     (f"{lid}:s", lid, campaign))
        if i < positive:
            conn.execute("INSERT INTO events (external_id, lead_id, campaign_id, type, reply_class) "
                         "VALUES (%s, %s, %s, 'reply', 'positive')", (f"{lid}:r", lid, campaign))


def test_pause_releases_only_unsent_leads(conn) -> None:
    bad, good = _theory(conn, "bad"), _theory(conn, "good")
    _with_events(conn, bad, 400, 0, "bad.example")
    _with_events(conn, good, 150, 6, "good.example")
    waiting = _sendable(conn, 3, bad, domain="wait.example")
    conn.execute("UPDATE leads SET status = 'queued' WHERE id = %s", (waiting[0],))
    conn.commit()
    health = {h.name: h.decision for h in evaluate_theories(conn)}
    assert health == {"bad": "pause", "good": "keep"}
    statuses = dict(conn.execute("SELECT status, count(*) FROM leads WHERE id = ANY(%s) GROUP BY 1", (waiting,)).fetchall())
    assert statuses == {"queued": 1, "scored": 2}  # the queued one keeps its theory and email
    assert conn.execute("SELECT count(*) FROM outgoing_messages WHERE lead_id = ANY(%s)", (waiting,)).fetchone()[0] == 2
    assert conn.execute("SELECT status FROM theories WHERE id = %s", (bad,)).fetchone()[0] == "paused"


def test_last_active_theory_is_never_paused(conn) -> None:
    only = _theory(conn, "only")
    _with_events(conn, only, 400, 0, "only.example")
    conn.commit()
    assert [h.decision for h in evaluate_theories(conn)] == ["kept_last_active"]
    assert conn.execute("SELECT status FROM theories").fetchone()[0] == "active"


# --- simulation end to end --------------------------------------------------------

def test_simulated_days_flow_through_the_signed_webhook(conn) -> None:
    from helpers import person, setup, world

    from leadengine.campaign import drain_copy, keyword_classifier, run_simulation
    from leadengine.copywriter import TemplateWriter
    from leadengine.drafting import prepare
    from leadengine.enrich import run_until_done
    from leadengine.seed.load import load_intake
    from leadengine.seed.theories import load_theories
    from leadengine.simulator import Simulator

    domains = [f"co{i:02d}.example" for i in range(30)]
    w = world(*(person(d, f"P{j}", "Berg", apollo=True, seniority="vp", title="VP Growth")
                for d in domains for j in range(2)))
    for c in w["companies"]:
        c.update(industry="saas", size_band="201-500", country="DE")
    load_intake(conn, w)
    conn.commit()
    run_until_done(conn, setup(w)[1])
    load_theories(conn)
    prepare(conn, None, copy_limit=0)
    drafted = drain_copy(conn, TemplateWriter(), "template")
    assert drafted > 0

    sim = Simulator(secret=SECRET, true_rates={"fresh funding, hiring for pipeline": 0.5,
                                               "new tool, broken attribution": 0.5, "new office, new market": 0.5})
    res = run_simulation(conn, sim, keyword_classifier(), days=6, writer=TemplateWriter(), mailboxes=1,
                         daily_limit=10)
    assert sum(d.rejected for d in res.days) == 0
    assert [d.queued for d in res.days][:2] == [10, 10]  # one mailbox, 10 a day
    sent_events = conn.execute("SELECT count(*) FROM events WHERE type = 'sent'").fetchone()[0]
    assert sent_events == min(drafted, 60)
    assert conn.execute("SELECT count(*) FROM events WHERE type = 'reply'").fetchone()[0] > 0
    assert conn.execute("SELECT count(*) FROM leads WHERE status = 'queued'").fetchone()[0] == 0


# --- theory generator ------------------------------------------------------------

GOOD_PROPOSALS = {"theories": [
    {"name": f"theory {i}", "hypothesis": "The company just raised money and is hiring, so the board will ask "
                                          "for pipeline numbers within a quarter.",
     "segment_criteria": {"industry": ["saas"], "seniority": ["vp"], "size_band": ["51-200"]},
     "required_variables": ["funding_round"]} for i in range(3)]}


def test_proposals_with_unfetchable_signals_or_unknown_segments_are_rejected() -> None:
    bad = json.loads(json.dumps(GOOD_PROPOSALS))
    bad["theories"][0]["required_variables"] = ["recent_linkedin_post"]
    bad["theories"][1]["segment_criteria"]["industry"] = ["healthcare"]
    bad["theories"][2]["name"] = "Fresh Funding"
    problems = validate_proposals(TheoryProposals(**bad), {"fresh funding"})
    assert any("not fetchable: ['recent_linkedin_post']" in p for p in problems)
    assert any("['healthcare'] do not exist" in p for p in problems)
    assert any("already taken" in p for p in problems)
    assert validate_proposals(TheoryProposals(**GOOD_PROPOSALS), set()) == []


def test_propose_stores_drafts_never_active(conn) -> None:
    _theory(conn, "existing")
    conn.commit()
    names = propose(conn, FakeLLM(lambda *_: json.dumps(GOOD_PROPOSALS)), "openai/gpt-oss-120b", [])
    assert names == ["theory 0", "theory 1", "theory 2"]
    assert {r[0] for r in conn.execute("SELECT status FROM theories WHERE name LIKE 'theory %'")} == {"draft"}


def test_propose_gives_up_after_one_repair(conn) -> None:
    bad = json.loads(json.dumps(GOOD_PROPOSALS))
    bad["theories"] = bad["theories"][:2]
    conn.commit()
    with pytest.raises(LLMInvalid, match="exactly 3"):
        propose(conn, FakeLLM(lambda *_: json.dumps(bad)), "m", [])
    assert conn.execute("SELECT count(*) FROM theories").fetchone()[0] == 0
