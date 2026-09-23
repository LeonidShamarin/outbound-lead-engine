"""Lead scoring against the ICP, 1 to 5.

The design sent every lead to an LLM with the rubric in the prompt. Every
criterion in that rubric that this data can answer is a structured field
(industry, size band, country, seniority), so the rubric is computed in code and
the LLM scorer exists to be compared with it (`--scoring llm`). The design's fourth
criterion, a recent signal, moved to theory assignment: signals cost money and are
now fetched only for leads that already passed scoring.

Rubric (the design's, restated on these fields):
  negative signal: under 50 employees (size band 11-50)          -> 1
  criteria: persona (director, VP or C-level), industry in ICP,
            size 51-1000, country in ICP
  all four and persona is VP or C-level                          -> 5
  all four, or three of four                                      -> 4
  two -> 3, one -> 2, none -> 1
Scores 3 and above are 'scored' (eligible), below go to 'cold_reserve'.
"""

from __future__ import annotations

from typing import Literal

import psycopg
from pydantic import BaseModel, Field

from leadengine.db import require_idle
from leadengine.llm import LLMCall, LLMClient, LLMError, LLMInvalid, structured

ICP_INDUSTRIES = ("saas", "ecommerce", "fintech", "martech")
ICP_SIZES = ("51-200", "201-500", "501-1000")
ICP_COUNTRIES = ("US", "GB", "DE", "NL", "ES", "CA")
PERSONA = ("c_level", "vp", "director")
MIN_ELIGIBLE = 3


def rubric_score(lead: dict) -> tuple[int, str]:
    if lead.get("size_band") == "11-50":
        return 1, "negative signal: under 50 employees"
    checks = {
        "persona": lead.get("seniority") in PERSONA,
        "industry": lead.get("industry") in ICP_INDUSTRIES,
        "size": lead.get("size_band") in ICP_SIZES,
        "country": lead.get("country") in ICP_COUNTRIES,
    }
    met = [k for k, ok in checks.items() if ok]
    n = len(met)
    if n == 4 and lead.get("seniority") in ("c_level", "vp"):
        score = 5
    elif n >= 3:
        score = 4
    else:
        score = {2: 3, 1: 2, 0: 1}[n]
    missing = [k for k in checks if k not in met]
    return score, f"meets {', '.join(met) or 'nothing'}" + (f"; misses {', '.join(missing)}" if missing else "")


class ScoreOut(BaseModel):
    score: int = Field(ge=1, le=5)
    rationale: str = Field(max_length=300)
    negative_signals: list[str]


SCORING_SYSTEM = """You score B2B sales leads against an Ideal Customer Profile (ICP) on a 1-5 scale.

ICP:
- Persona: Director, Head, VP or C-level (seniority director, vp or c_level)
- Industry: saas, ecommerce, fintech, martech
- Company size band: 51-200, 201-500 or 501-1000 employees
- Country: US, GB, DE, NL, ES, CA

Negative signal: size band 11-50 (under 50 employees). If present, the score is 1.

Rubric (count how many of the four criteria persona, industry, size, country match):
- 5 = all four match AND seniority is vp or c_level
- 4 = all four match, or three of four match
- 3 = two match
- 2 = one matches
- 1 = none match, or the negative signal is present

Answer in JSON. rationale: one sentence naming the criteria met and missed."""


def _user_prompt(lead: dict) -> str:
    return (f"Lead:\n- Title: {lead.get('title')}\n- Seniority: {lead.get('seniority')}\n"
            f"Company:\n- Industry: {lead.get('industry')}\n- Size band: {lead.get('size_band')}\n"
            f"- Country: {lead.get('country')}\n\nScore this lead.")


def llm_score(llm: LLMClient, model: str, lead: dict, log: list[LLMCall]) -> tuple[int, str]:
    out = structured(llm, ScoreOut, step="score", model=model, system=SCORING_SYSTEM,
                     user=_user_prompt(lead), log=log, lead_id=str(lead["id"]))
    return out.score, out.rationale


def score_leads(
    conn: psycopg.Connection,
    *,
    mode: Literal["rules", "llm"] = "rules",
    llm: LLMClient | None = None,
    model: str = "openai/gpt-oss-20b",
    batch: int = 100,
    log: list[LLMCall] | None = None,
) -> dict[str, int]:
    """Score up to `batch` enriched leads. With mode='llm', a failed LLM call falls back
    to the rubric for that lead, and the fallback is counted, not hidden."""
    if mode == "llm" and llm is None:
        raise ValueError("mode='llm' needs an llm client")
    require_idle(conn, "score_leads")
    log = log if log is not None else []
    stats = {"scored": 0, "cold_reserve": 0, "unverified": 0, "llm_fallback": 0}
    with conn.transaction():
        conn.execute("SELECT release_stale_leads('scoring', 'enriched', '15 minutes'::interval)")
        # An address that did not verify is never sent to; scoring it would only cost money.
        stats["unverified"] = conn.execute(
            """UPDATE leads SET status = 'cold_reserve', last_error = 'email not verified'
                WHERE status = 'enriched' AND NOT email_verified"""
        ).rowcount
        rows = conn.execute(
            """SELECT l.id, l.title, l.seniority, c.industry, c.size_band, c.country
                 FROM claim_leads('enriched', 'scoring', %s) l JOIN companies c ON c.id = l.company_id""",
            (batch,),
        ).fetchall()
    cols = ("id", "title", "seniority", "industry", "size_band", "country")
    for row in rows:
        lead = dict(zip(cols, row))
        if mode == "llm":
            try:
                score, why = llm_score(llm, model, lead, log)
            except (LLMError, LLMInvalid) as e:
                score, why = rubric_score(lead)
                why = f"rubric fallback ({type(e).__name__}): {why}"
                stats["llm_fallback"] += 1
        else:
            score, why = rubric_score(lead)
        status = "scored" if score >= MIN_ELIGIBLE else "cold_reserve"
        with conn.transaction():
            conn.execute("UPDATE leads SET lead_score = %s, lead_score_rationale = %s, status = %s WHERE id = %s",
                         (score, why, status, lead["id"]))
        stats[status] += 1
    return stats
