"""Propose new theories from reply statistics, as drafts a human activates.

The design ran this weekly and let the model name any "fetchable" variable,
including LinkedIn activity, which this project does not automate. Here the
validator allows only signal keys the pipeline can actually fetch
(`signals.SOURCES`), segment values that exist in the data, and names that are
not taken. Proposals are stored as 'draft': a theory decides who gets emailed,
so turning one on stays a human decision.
"""

from __future__ import annotations

import json

import psycopg
from pydantic import BaseModel, Field

from leadengine.llm import LLMCall, LLMClient, structured
from leadengine.seed.world import INDUSTRIES, SIZE_BANDS
from leadengine.signals import SOURCES

SENIORITIES = ("c_level", "vp", "director", "manager", "ic")


class Segment(BaseModel):
    industry: list[str]
    seniority: list[str]
    size_band: list[str]


class TheoryDraft(BaseModel):
    name: str = Field(max_length=60)
    hypothesis: str = Field(max_length=400)
    segment_criteria: Segment
    required_variables: list[str]


class TheoryProposals(BaseModel):
    theories: list[TheoryDraft]


def segment_stats(conn: psycopg.Connection, min_sent: int = 20) -> list[dict]:
    rows = conn.execute(
        """SELECT c.industry, l.seniority, c.size_band,
                  count(DISTINCT l.id) FILTER (WHERE e.type = 'sent') AS sent,
                  count(DISTINCT l.id) FILTER (WHERE e.reply_class = 'positive') AS positive
             FROM leads l JOIN companies c ON c.id = l.company_id JOIN events e ON e.lead_id = l.id
            GROUP BY 1, 2, 3 HAVING count(DISTINCT l.id) FILTER (WHERE e.type = 'sent') >= %s
            ORDER BY 1, 2, 3""", (min_sent,)).fetchall()
    return [{"industry": i, "seniority": s, "size_band": b, "n": n, "positive_rate": round(p / n, 4)}
            for i, s, b, n, p in rows]


def validate_proposals(p: TheoryProposals, taken_names: set[str]) -> list[str]:
    problems = []
    if len(p.theories) != 3:
        problems.append(f"propose exactly 3 theories, got {len(p.theories)}")
    names = set()
    for i, t in enumerate(p.theories):
        tag = f"theory {i + 1}"
        if t.name.strip().lower() in taken_names | names:
            problems.append(f"{tag}: name {t.name!r} is already taken")
        names.add(t.name.strip().lower())
        bad_vars = [v for v in t.required_variables if v not in SOURCES]
        if bad_vars or not 1 <= len(t.required_variables) <= 3:
            problems.append(f"{tag}: required_variables must be 1 to 3 of {sorted(SOURCES)}; not fetchable: {bad_vars}")
        for field, allowed in (("industry", INDUSTRIES), ("seniority", SENIORITIES), ("size_band", SIZE_BANDS)):
            bad = [v for v in getattr(t.segment_criteria, field) if v not in allowed]
            if bad:
                problems.append(f"{tag}: {field} values {bad} do not exist; allowed {list(allowed)}")
        if t.hypothesis.count(".") < 1 or len(t.hypothesis.split()) < 12:
            problems.append(f"{tag}: hypothesis must explain why now in one or two full sentences")
    return problems


SYSTEM = f"""You are a senior B2B outbound strategist.
Given reply statistics per segment and the theories already tested, propose 3 NEW theories about why a person
in a segment would answer a cold email about pipeline attribution RIGHT NOW.

Each theory:
- name: 5 words max, lowercase, specific
- hypothesis: 1 or 2 sentences naming WHAT changed at the company and WHY that makes now the moment
- segment_criteria: industry, seniority, size_band lists. An empty list means "any".
  industry values: {list(INDUSTRIES)}; seniority values: {list(SENIORITIES)}; size_band values: {list(SIZE_BANDS)}
- required_variables: 1 to 3 signal keys, ONLY from: {sorted(SOURCES)}. Nothing else can be fetched.

Build on segments with high positive_rate; for weak segments, try a different trigger, not the same one again.
Avoid generic theories like "the company is growing"."""


def propose(conn: psycopg.Connection, llm: LLMClient, model: str, log: list[LLMCall]) -> list[str]:
    """Ask for 3 theories, validate, store as drafts. Returns their names."""
    existing = conn.execute(
        "SELECT name, hypothesis, status, sample_size, positive_reply_rate FROM theories ORDER BY name").fetchall()
    conn.commit()
    taken = {r[0].strip().lower() for r in existing}
    user = ("Segment stats (sent leads, positive reply rate):\n" + json.dumps(segment_stats(conn), indent=1) +
            "\n\nTheories already tested:\n" + json.dumps(
                [{"name": n, "hypothesis": h, "status": s, "sent": ss, "positive_pct": float(p) if p is not None else None}
                 for n, h, s, ss, p in existing], indent=1) + "\n\nPropose 3 new theories.")
    conn.commit()
    out = structured(llm, TheoryProposals, step="theory", model=model, system=SYSTEM, user=user, log=log,
                     validate=lambda p: validate_proposals(p, taken), temperature=0.7)
    with conn.transaction():
        for t in out.theories:
            crit = {k: v for k, v in t.segment_criteria.model_dump().items() if v}
            conn.execute(
                """INSERT INTO theories (name, hypothesis, segment_criteria, required_variables, status)
                   VALUES (%s, %s, %s, %s, 'draft')""",
                (t.name.strip().lower(), t.hypothesis.strip(), json.dumps(crit), json.dumps(t.required_variables)))
    return [t.name.strip().lower() for t in out.theories]
