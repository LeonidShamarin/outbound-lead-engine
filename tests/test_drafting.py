"""Stage 3 steps against a real Postgres: scoring, signals, theories, copy."""

from __future__ import annotations

import json
import re

import psycopg
import pytest
from test_copywriter import GOOD_A, GOOD_B

from leadengine.drafting import assign, draft_copy
from leadengine.llm import FakeLLM, LLMError
from leadengine.scoring import score_leads
from leadengine.seed.theories import load_theories
from leadengine.signals import fetch_company_signals

FUNDING = "Series B, $24M, announced in June 2026"
HIRING = "7 open sales and marketing roles posted in the 30 days before 2026-09-01"
VALID_COPY = json.dumps({"subject": "your series b and hiring", "body_a": GOOD_A, "body_b": GOOD_B,
                         "approach_a": "observation", "approach_b": "question"})


def _company(conn, domain="bluestack.example", name="Bluestack", industry="saas", size="201-500", country="DE"):
    return conn.execute(
        """INSERT INTO companies (domain, name, industry, size_band, country, status)
           VALUES (%s, %s, %s, %s, %s, 'enriched') RETURNING id""",
        (domain, name, industry, size, country)).fetchone()[0]


def _lead(conn, company, email, seniority="vp", verified=True, first="Anna", last="Keller", title="VP Marketing"):
    return conn.execute(
        """INSERT INTO leads (company_id, first_name, last_name, title, seniority, email, email_verified, status)
           VALUES (%s, %s, %s, %s, %s, %s, %s, 'enriched') RETURNING id""",
        (company, first, last, title, seniority, email, verified)).fetchone()[0]


def _signals(conn, company, **values):
    for key in ("funding_round", "hiring_pace", "tech_stack_change", "company_news"):
        conn.execute("INSERT INTO company_signals (company_id, key, value, source) VALUES (%s, %s, %s, 'test')",
                     (company, key, values.get(key)))


def _status(conn, lead):
    return conn.execute("SELECT status FROM leads WHERE id = %s", (lead,)).fetchone()[0]


def test_score_3_lead_reaches_variables_ready_instead_of_waiting_forever(conn) -> None:
    load_theories(conn)
    c = _company(conn, industry="logistics")
    lead = _lead(conn, c, "ic@bluestack.example", seniority="ic", title="Marketing Analyst")
    conn.commit()
    score_leads(conn)
    assert conn.execute("SELECT lead_score FROM leads").fetchone()[0] == 3
    _signals(conn, c, company_news="opened an office in Lisbon in June 2026")
    conn.commit()
    r = assign(conn)
    assert r == {"assigned": 1, "no_theory": 0, "variables_ready": 1}
    assert _status(conn, lead) == "variables_ready"


def test_unverified_email_is_not_scored(conn) -> None:
    lead = _lead(conn, _company(conn), "risky@bluestack.example", verified=False)
    conn.commit()
    s = score_leads(conn)
    assert s["unverified"] == 1 and s["scored"] == 0
    assert conn.execute("SELECT status, lead_score FROM leads WHERE id = %s", (lead,)).fetchone() == ("cold_reserve", None)


def test_llm_scoring_falls_back_to_the_rubric_and_says_so(conn) -> None:
    _lead(conn, _company(conn), "a@bluestack.example")
    conn.commit()

    class Down:
        def complete(self, **_):
            raise LLMError("groq down")

    s = score_leads(conn, mode="llm", llm=Down())
    score, why = conn.execute("SELECT lead_score, lead_score_rationale FROM leads").fetchone()
    assert s["llm_fallback"] == 1 and score == 5 and why.startswith("rubric fallback")


def test_signals_are_fetched_once_per_company_not_per_lead(conn) -> None:
    c = _company(conn)
    for i in range(3):
        _lead(conn, c, f"p{i}@bluestack.example")
    conn.commit()
    score_leads(conn)
    assert fetch_company_signals(conn) == 1
    assert fetch_company_signals(conn) == 0
    assert conn.execute("SELECT count(*) FROM company_signals").fetchone()[0] == 4


def test_lead_with_no_fitting_theory_goes_to_cold_reserve_with_a_reason(conn) -> None:
    load_theories(conn)
    c = _company(conn)
    lead = _lead(conn, c, "a@bluestack.example")
    conn.commit()
    score_leads(conn)
    _signals(conn, c)  # looked, found nothing
    conn.commit()
    assert assign(conn)["no_theory"] == 1
    status, err = conn.execute("SELECT status, last_error FROM leads WHERE id = %s", (lead,)).fetchone()
    assert status == "cold_reserve" and "no active theory" in err


def test_best_theory_needs_all_its_variables(conn) -> None:
    load_theories(conn)
    c = _company(conn)
    lead = _lead(conn, c, "a@bluestack.example")
    conn.commit()
    score_leads(conn)
    # funding without hiring: the funding theory does not fit, the tool theory does
    _signals(conn, c, funding_round=FUNDING, tech_stack_change="added Segment to its stack in June 2026")
    conn.commit()
    assign(conn)
    theory, keys = conn.execute(
        """SELECT t.name, (SELECT array_agg(v.key ORDER BY v.key) FROM variables v WHERE v.lead_id = l.id)
             FROM leads l JOIN theories t ON t.id = l.theory_id WHERE l.id = %s""", (lead,)).fetchone()
    assert theory == "new tool, broken attribution" and keys == ["tech_stack_change"]


def _ready_lead(conn) -> str:
    load_theories(conn)
    c = _company(conn)
    lead = _lead(conn, c, "anna.keller@bluestack.example")
    conn.commit()
    score_leads(conn)
    _signals(conn, c, funding_round=FUNDING, hiring_pace=HIRING)
    conn.commit()
    assign(conn)
    assert _status(conn, lead) == "variables_ready"
    conn.commit()
    return lead


def test_copy_is_stored_as_two_variants_and_costed(conn) -> None:
    lead = _ready_lead(conn)
    llm = FakeLLM(lambda *_: VALID_COPY, prompt_tokens=1200, completion_tokens=400)
    s = draft_copy(conn, llm, model="openai/gpt-oss-120b")
    assert s["copy_ready"] == 1
    msgs = conn.execute("SELECT variant, subject FROM outgoing_messages ORDER BY variant").fetchall()
    assert msgs == [("A", "your series b and hiring"), ("B", "your series b and hiring")]
    assert _status(conn, lead) == "copy_ready"
    outcome, cost = conn.execute("SELECT outcome, cost_usd FROM llm_calls WHERE step = 'copy'").fetchone()
    assert outcome == "ok" and float(cost) == pytest.approx((1200 * 0.15 + 400 * 0.60) / 1e6)
    # the prompt carried the lead's real variables
    assert "Series B, $24M" in llm.requests[0]["user"]


def test_invented_number_is_repaired_before_storing(conn) -> None:
    _ready_lead(conn)
    bad = json.loads(VALID_COPY)
    bad["body_a"] = GOOD_A.replace("7 sales", "40 sales")
    answers = iter([json.dumps(bad), VALID_COPY])
    s = draft_copy(conn, FakeLLM(lambda *_: next(answers)))
    assert s == {"copy_ready": 1, "retry": 0, "dead_letter": 0, "repaired": 1}
    assert "40" not in conn.execute("SELECT body FROM outgoing_messages WHERE variant = 'A'").fetchone()[0]


def test_copy_that_never_validates_goes_to_dead_letter_once(conn) -> None:
    lead = _ready_lead(conn)
    bad = json.loads(VALID_COPY)
    bad["body_b"] = bad["body_a"]
    llm = FakeLLM(lambda *_: json.dumps(bad))
    results = []
    for _ in range(4):
        results.append(draft_copy(conn, llm))
        conn.commit()
    assert [r["retry"] for r in results] == [1, 1, 0, 0]
    assert [r["dead_letter"] for r in results] == [0, 0, 1, 0]
    assert _status(conn, lead) == "dead_letter"
    assert conn.execute("SELECT count(*) FROM outgoing_messages").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM llm_calls WHERE outcome = 'invalid'").fetchone()[0] == 6


def test_stale_drafting_claim_is_released(conn) -> None:
    lead = _ready_lead(conn)
    conn.execute("SELECT claim_leads('variables_ready', 'drafting', 10)")
    conn.execute("UPDATE leads SET last_attempt_at = now() - interval '1 hour'")
    conn.commit()
    s = draft_copy(conn, FakeLLM(lambda *_: VALID_COPY))
    assert s["copy_ready"] == 1 and _status(conn, lead) == "copy_ready"
    assert conn.execute("SELECT attempts FROM leads WHERE id = %s", (lead,)).fetchone()[0] == 1


def _email_from_prompt(schema_name: str, system: str, user: str) -> str:
    """A fake model that writes a valid email for any lead, from the prompt alone."""
    company = re.search(r"Company: (.+?) \(", user).group(1)
    variables = json.loads(user.split("Variables (use the most specific one or two):\n", 1)[1].rsplit("\n\nWrite", 1)[0])
    fact = next(iter(variables.values()))
    return json.dumps({
        "subject": "a question about pipeline",
        "body_a": f"Saw that {company} {fact}.\n\nTallybird shows which channel produced which pipeline.\n\nIs that on your radar?",
        "body_b": f"Question: after {company} {fact}, who owns channel attribution?\n\nTallybird answers that.\n\nWorth a chat?",
        "approach_a": "observation", "approach_b": "question",
    })


def test_prepare_takes_a_world_from_enriched_to_copy_ready(conn, tmp_path) -> None:
    from helpers import person, setup, world

    from leadengine.drafting import prepare
    from leadengine.enrich import run_until_done
    from leadengine.evaluate import dump_copy
    from leadengine.seed.load import load_intake

    w = world(*(person(f"{n}.example", f"P{i}", "Novak", apollo=True, seniority=s, title=t)
                for n in ("northwind", "bluestack", "cedarflow", "atlasgrid", "riverlabs", "quietfield")
                for i, (s, t) in enumerate((("vp", "VP Marketing"), ("ic", "SDR")))))
    for c in w["companies"]:
        c.update(industry="saas", size_band="201-500", country="DE")
    load_intake(conn, w)
    conn.commit()
    _, clients, _, _ = setup(w)
    run_until_done(conn, clients)
    load_theories(conn)

    llm = FakeLLM(_email_from_prompt)
    s = prepare(conn, llm, copy_limit=50)
    assert s.score["scored"] == 12  # vp: 5, ic at an ICP company: 4
    ready = s.assign["variables_ready"]
    assert ready > 0 and s.assign["assigned"] + s.assign["no_theory"] == 12
    assert s.copy == {"copy_ready": ready, "retry": 0, "dead_letter": 0, "repaired": 0}
    summary = dump_copy(conn, "openai/gpt-oss-120b", results_dir=tmp_path)
    assert summary["emails_stored"] == ready and summary["first_try_valid"] == ready
    assert (tmp_path / "copy-gpt-oss-120b.json").exists()
    # every address in this world verified, so every drafted lead is sendable
    assert conn.execute("SELECT count(*) FROM leads_sendable").fetchone()[0] == ready


def test_referral_reply_class_is_accepted_by_the_schema(conn) -> None:

    lead = _lead(conn, _company(conn), "a@bluestack.example")
    conn.execute("INSERT INTO events (external_id, lead_id, type, reply_class) VALUES ('r1', %s, 'reply', 'referral')",
                 (lead,))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO events (external_id, lead_id, type, reply_class) VALUES ('r2', %s, 'reply', 'maybe')",
                     (lead,))
