"""Stage 5: the deployed app, the dashboard views, and the scheduled cycle."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from leadengine.killswitch import wilson
from leadengine.webhook import sign

SECRET = "whsec_web"


@pytest.fixture
def client(conn, db_url, monkeypatch) -> TestClient:
    conn.commit()
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    from leadengine.web import app
    return TestClient(app)


def test_dashboard_renders_on_an_empty_database(client) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "Outbound Lead Engine" in r.text and "nothing yet" in r.text


def test_health_and_dashboard_without_a_database(client, monkeypatch) -> None:
    assert client.get("/health").json() == {"ok": True, "db": True}
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@127.0.0.1:1/x")
    r = client.get("/health")
    assert r.status_code == 503 and r.json()["db"] is False
    page = client.get("/")
    assert page.status_code == 503 and "not reachable" in page.text


def test_data_is_escaped_in_html(client, conn) -> None:
    conn.execute("INSERT INTO theories (name, hypothesis, status) VALUES ('<script>alert(1)</script>', 'h', 'draft')")
    conn.commit()
    page = client.get("/").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def _queued(conn):
    from leadengine.sending import ensure_mailboxes, queue_for_sending
    t = conn.execute("INSERT INTO theories (name, hypothesis, status) VALUES ('t', 'h', 'active') RETURNING id").fetchone()[0]
    c = conn.execute("INSERT INTO companies (domain, name, status) VALUES ('web.example', 'Web', 'enriched') RETURNING id").fetchone()[0]
    lead = conn.execute("""INSERT INTO leads (company_id, first_name, email, email_verified, status, theory_id, lead_score)
                           VALUES (%s, 'Ann', 'ann@web.example', true, 'copy_ready', %s, 5) RETURNING id""", (c, t)).fetchone()[0]
    for v in "AB":
        conn.execute("INSERT INTO outgoing_messages (lead_id, theory_id, variant, subject, body) VALUES (%s, %s, %s, 's', 'b')",
                     (lead, t, v))
    conn.commit()
    ensure_mailboxes(conn)
    return queue_for_sending(conn)[0]


def test_webhook_over_http(client, conn) -> None:
    q = _queued(conn)
    body = json.dumps({"id": "w1", "type": "reply", "lead_email": q.email, "campaign_id": q.campaign_external_id,
                       "provider": "simulator", "text": "Interested, send pricing"}).encode()
    assert client.post("/api/events", content=body).status_code == 401
    r = client.post("/api/events", content=body, headers={"X-Signature": sign(SECRET, body)})
    assert r.status_code == 200 and r.json() == {"ok": True, "duplicate": False}
    again = client.post("/api/events", content=body, headers={"X-Signature": sign(SECRET, body)})
    assert again.json()["duplicate"] is True
    assert conn.execute("SELECT reply_class, payload->>'classifier' FROM events").fetchone() == ("positive", "keywords")
    feed = client.get("/api/summary").json()["feed"]
    assert feed[0]["first_name"] == "Ann" and "pricing" in feed[0]["reply_text"]


def test_oversized_body_is_refused_before_reading(client) -> None:
    r = client.post("/api/events", content=b"x" * (64 * 1024 + 1), headers={"X-Signature": "t=1,v1=0"})
    assert r.status_code == 413


def test_theory_view_wilson_matches_python(conn) -> None:
    t = conn.execute("INSERT INTO theories (name, hypothesis, status) VALUES ('w', 'h', 'active') RETURNING id").fetchone()[0]
    c = conn.execute("INSERT INTO companies (domain) VALUES ('w.example') RETURNING id").fetchone()[0]
    camp = conn.execute("INSERT INTO campaigns (theory_id, external_campaign_id, provider) VALUES (%s, 'cw', 'simulator') "
                        "RETURNING id", (t,)).fetchone()[0]
    for i in range(40):
        lead = conn.execute("INSERT INTO leads (company_id, email, status) VALUES (%s, %s, 'sent') RETURNING id",
                            (c, f"p{i}@w.example")).fetchone()[0]
        conn.execute("INSERT INTO events (external_id, lead_id, campaign_id, type) VALUES (%s, %s, %s, 'sent')",
                     (f"s{i}", lead, camp))
        if i < 3:
            conn.execute("INSERT INTO events (external_id, lead_id, campaign_id, type, reply_class) "
                         "VALUES (%s, %s, %s, 'reply', 'positive')", (f"r{i}", lead, camp))
    lo, hi, sent, pos = conn.execute("SELECT wilson_lower, wilson_upper, sent, positive FROM v_theories").fetchone()
    assert (sent, pos) == (40, 3)
    assert float(lo) == pytest.approx(wilson(3, 40)[0], abs=1e-5)
    assert float(hi) == pytest.approx(wilson(3, 40)[1], abs=1e-5)
    funnel = dict(zip([d.name for d in conn.execute("SELECT * FROM v_funnel").description],
                      conn.execute("SELECT * FROM v_funnel").fetchone()))
    assert (funnel["sent"], funnel["positive"], funnel["replied"]) == (40, 3, 3)


def test_scheduled_days_keep_the_simulators_promises(conn, client) -> None:
    from helpers import person, setup, world

    from leadengine.campaign import keyword_classifier, run_scheduled_day
    from leadengine.copywriter import TemplateWriter
    from leadengine.enrich import run_until_done
    from leadengine.seed.load import load_intake

    w = world(*(person(f"s{i}.example", "A", f"B{i}", apollo=True, seniority="vp", title="VP Growth") for i in range(40)))
    for c in w["companies"]:
        c.update(industry="saas", size_band="201-500", country="DE")
    load_intake(conn, w)
    conn.commit()
    run_until_done(conn, setup(w)[1])

    # Deliver over "HTTP" into the app, the same path a deployed cycle uses.
    def post(url, content, headers, timeout):
        return client.post("/api/events", content=content, headers=headers)

    from datetime import datetime, timedelta
    days = [run_scheduled_day(conn, secret=SECRET, classify=keyword_classifier(), writer=TemplateWriter(),
                              writer_model="template", copy_limit=50, webhook_url="http://app/api/events",
                              post=post, mailboxes=1, daily_limit=5, now=datetime(2026, 10, 1) + timedelta(days=i))
            for i in range(6)]
    assert [d.day for d in days] == [0, 1, 2, 3, 4, 5]
    assert all(set(d.statuses) <= {200} for d in days)
    assert [d.queued for d in days][:2] == [5, 5]
    sent = conn.execute("SELECT count(*) FROM events WHERE type = 'sent'").fetchone()[0]
    assert sent == sum(d.queued for d in days)
    # replies come 1 to 4 days later; what is not due yet waits in the outbox
    assert conn.execute("SELECT count(*) FROM sim_outbox WHERE due_day <= 5").fetchone()[0] == 0
