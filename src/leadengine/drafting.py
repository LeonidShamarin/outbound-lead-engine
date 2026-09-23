"""Pipeline steps after enrichment: score, signals, theories, copy.

Each step claims its input with SKIP LOCKED, commits the claim, does the slow work
outside a transaction, and writes its result in one transaction per lead. A failed
LLM answer goes through mark_lead_failed: back to the input status while attempts
remain, then dead_letter once.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import psycopg

from leadengine.db import require_idle
from leadengine.copywriter import COPY_SYSTEM, CopyOut, LeadContext, copy_user_prompt, validate_copy
from leadengine.llm import LLMCall, LLMClient, LLMError, LLMInvalid, structured
from leadengine.scoring import score_leads
from leadengine.signals import fetch_company_signals


def write_llm_calls(conn: psycopg.Connection, run_id: str, calls: list[LLMCall]) -> None:
    if not calls:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO llm_calls (run_id, lead_id, step, model, prompt_tokens, completion_tokens,
                                      cost_usd, latency_ms, outcome, error)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [(run_id, c.lead_id, c.step, c.model, c.prompt_tokens, c.completion_tokens, c.cost_usd,
              c.latency_ms, c.outcome, c.error) for c in calls],
        )


def assign(conn: psycopg.Connection, batch: int = 200) -> dict[str, int]:
    require_idle(conn, "assign")
    with conn.transaction():
        assigned, no_theory = conn.execute("SELECT * FROM assign_theories(%s)", (batch,)).fetchone()
        ready = conn.execute("SELECT mark_variables_ready()").fetchone()[0]
    return {"assigned": assigned, "no_theory": no_theory, "variables_ready": ready}


_CONTEXT_SQL = """
SELECT l.id, l.first_name, l.last_name, l.title, c.name, c.domain, c.industry, c.size_band, c.country,
       t.id, t.name, t.hypothesis,
       COALESCE(jsonb_object_agg(v.key, v.value) FILTER (WHERE v.key IS NOT NULL), '{}'::jsonb)
  FROM leads l
  JOIN companies c ON c.id = l.company_id
  JOIN theories t ON t.id = l.theory_id
  LEFT JOIN variables v ON v.lead_id = l.id AND v.theory_id = l.theory_id
 WHERE l.id = ANY(%s)
 GROUP BY l.id, c.id, t.id
"""


def draft_copy(
    conn: psycopg.Connection,
    llm: LLMClient,
    *,
    model: str = "openai/gpt-oss-120b",
    batch: int = 25,
    max_attempts: int = 3,
    run_id: str | None = None,
    temperature: float = 0.7,
) -> dict[str, int]:
    require_idle(conn, "draft_copy")
    run_id = run_id or uuid.uuid4().hex[:12]
    stats = {"copy_ready": 0, "retry": 0, "dead_letter": 0, "repaired": 0}
    with conn.transaction():
        conn.execute("SELECT release_stale_leads('drafting', 'variables_ready', '15 minutes'::interval, %s)",
                     (max_attempts,))
        ids = [r[0] for r in conn.execute("SELECT id FROM claim_leads('variables_ready', 'drafting', %s)", (batch,))]
        rows = conn.execute(_CONTEXT_SQL, (ids,)).fetchall() if ids else []

    for r in rows:
        ctx = LeadContext(lead_id=str(r[0]), first_name=r[1], last_name=r[2], title=r[3], company_name=r[4],
                          domain=r[5], industry=r[6], size_band=r[7], country=r[8], theory_name=r[10],
                          hypothesis=r[11], variables=dict(r[12]))
        theory_id = r[9]
        calls: list[LLMCall] = []
        try:
            out = structured(llm, CopyOut, step="copy", model=model, system=COPY_SYSTEM,
                             user=copy_user_prompt(ctx), log=calls, lead_id=ctx.lead_id,
                             validate=lambda c: validate_copy(c, ctx), temperature=temperature)
        except (LLMError, LLMInvalid) as e:
            with conn.transaction():
                dead = conn.execute("SELECT mark_lead_failed(%s, 'copy', %s, 'variables_ready', %s)",
                                    (ctx.lead_id, str(e)[:500], max_attempts)).fetchone()[0]
                write_llm_calls(conn, run_id, calls)
            stats["dead_letter" if dead else "retry"] += 1
            continue
        with conn.transaction():
            for variant, body in (("A", out.body_a), ("B", out.body_b)):
                conn.execute(
                    """INSERT INTO outgoing_messages (lead_id, theory_id, variant, subject, body, model)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (lead_id, variant) DO UPDATE
                          SET subject = EXCLUDED.subject, body = EXCLUDED.body, model = EXCLUDED.model""",
                    (ctx.lead_id, theory_id, variant, out.subject.strip(), body.strip(), model),
                )
            conn.execute("UPDATE leads SET status = 'copy_ready' WHERE id = %s AND status = 'drafting'",
                         (ctx.lead_id,))
            write_llm_calls(conn, run_id, calls)
        stats["copy_ready"] += 1
        stats["repaired"] += int(calls[-1].outcome == "repaired")
    return stats


@dataclass
class PrepareStats:
    score: dict = field(default_factory=dict)
    signals: int = 0
    assign: dict = field(default_factory=dict)
    copy: dict = field(default_factory=dict)


def prepare(conn: psycopg.Connection, llm: LLMClient | None, *, scoring: str = "rules",
            score_model: str = "openai/gpt-oss-20b", copy_model: str = "openai/gpt-oss-120b",
            copy_limit: int = 25, max_rounds: int = 1000, run_id: str | None = None) -> PrepareStats:
    """Enriched leads -> copy_ready, for at most `copy_limit` emails. Bounded by max_rounds."""
    run_id = run_id or uuid.uuid4().hex[:12]
    s = PrepareStats()
    log: list[LLMCall] = []
    for _ in range(max_rounds):
        r = score_leads(conn, mode=scoring, llm=llm, model=score_model, log=log)
        for k, v in r.items():
            s.score[k] = s.score.get(k, 0) + v
        if not any(r.values()):
            break
    else:
        raise RuntimeError("scoring did not drain")
    with conn.transaction():
        write_llm_calls(conn, run_id, log)
    for _ in range(max_rounds):
        n = fetch_company_signals(conn)
        s.signals += n
        a = assign(conn)
        for k, v in a.items():
            s.assign[k] = s.assign.get(k, 0) + v
        if n == 0 and a["assigned"] == 0 and a["no_theory"] == 0:
            break
    else:
        raise RuntimeError("theory assignment did not drain")
    if llm is not None and copy_limit > 0:
        s.copy = draft_copy(conn, llm, model=copy_model, batch=copy_limit, run_id=run_id)
    return s
