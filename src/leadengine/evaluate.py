"""Evals against the real LLM: reply classification and scoring agreement.

Both write their full per-item results to eval/results/, so the numbers in the
README can be traced to individual answers.
"""

from __future__ import annotations

import json
import random
import time
from collections import Counter
from pathlib import Path

from leadengine.llm import LLMCall, LLMClient, LLMError, LLMInvalid
from leadengine.replies import CATEGORIES, classify_keywords, classify_llm
from leadengine.scoring import llm_score, rubric_score

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "eval" / "results"


def _save(name: str, payload: dict, results_dir: Path = RESULTS) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / name
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    return path


def _shown(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def eval_replies(llm: LLMClient, model: str, pause_s: float = 0.0) -> dict:
    rows = [json.loads(x) for x in (ROOT / "eval" / "replies.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    log: list[LLMCall] = []
    items = []
    for r in rows:
        kw, _ = classify_keywords(r["text"])
        try:
            out = classify_llm(llm, model, r["text"], log)
            pred, ref, err = out.category, out.referral_email, None
        except (LLMError, LLMInvalid) as e:
            pred, ref, err = "error", None, str(e)[:200]
        items.append({"id": r["id"], "label": r["label"], "llm": pred, "keywords": kw, "referral_email": ref,
                      "error": err, "text": r["text"]})
        if pause_s:
            time.sleep(pause_s)
    n = len(items)
    per_class = {c: {"n": sum(i["label"] == c for i in items),
                     "llm_correct": sum(i["label"] == c == i["llm"] for i in items),
                     "keywords_correct": sum(i["label"] == c == i["keywords"] for i in items)} for c in CATEGORIES}
    summary = {
        "model": model, "n": n,
        "llm_accuracy": round(sum(i["label"] == i["llm"] for i in items) / n, 3),
        "keywords_accuracy": round(sum(i["label"] == i["keywords"] for i in items) / n, 3),
        "llm_errors": sum(i["llm"] == "error" for i in items),
        "repaired": sum(c.outcome == "repaired" for c in log),
        "requests": len(log),
        "cost_usd": round(sum(c.cost_usd for c in log), 6),
        "avg_latency_ms": round(sum(c.latency_ms for c in log) / max(len(log), 1)),
        "per_class": per_class,
        "confusions": dict(Counter(f"{i['label']}->{i['llm']}" for i in items if i["label"] != i["llm"])),
    }
    summary["results_file"] = str(_save(f"replies-{model.split('/')[-1]}.json",
                                        {"summary": summary, "items": items}).relative_to(ROOT))
    return summary


def dump_copy(conn, model: str, results_dir: Path = RESULTS) -> dict:
    """Every generated email with its lead context, plus how generation went."""
    rows = conn.execute(
        """SELECT l.id, l.first_name, l.title, c.name, c.domain, t.name,
                  (SELECT jsonb_object_agg(v.key, v.value) FROM variables v WHERE v.lead_id = l.id),
                  a.subject, a.body, b.body
             FROM leads l
             JOIN companies c ON c.id = l.company_id
             JOIN theories t ON t.id = l.theory_id
             JOIN outgoing_messages a ON a.lead_id = l.id AND a.variant = 'A'
             JOIN outgoing_messages b ON b.lead_id = l.id AND b.variant = 'B'
            ORDER BY c.domain, l.id""").fetchall()
    calls = conn.execute(
        """SELECT count(*), count(DISTINCT lead_id), coalesce(sum(cost_usd), 0)::float,
                  count(*) FILTER (WHERE outcome = 'ok'), count(*) FILTER (WHERE outcome = 'repaired'),
                  count(*) FILTER (WHERE outcome = 'invalid'), count(*) FILTER (WHERE outcome = 'error'),
                  coalesce(avg(latency_ms), 0)::int, coalesce(sum(prompt_tokens), 0), coalesce(sum(completion_tokens), 0)
             FROM llm_calls WHERE step = 'copy'""").fetchone()
    reasons = Counter()
    for (err,) in conn.execute("SELECT error FROM llm_calls WHERE step = 'copy' AND outcome = 'invalid'"):
        for part in (err or "").split("; "):
            reasons[part.split(":")[0].split(" has ")[0].strip()[:60]] += 1
    dead = conn.execute("SELECT count(*) FROM leads WHERE status = 'dead_letter'").fetchone()[0]
    requests, leads, cost, ok, repaired, invalid, errors, latency, pt, ct = calls
    emails = [{"lead": str(r[0]), "first_name": r[1], "title": r[2], "company": r[3], "domain": r[4], "theory": r[5],
               "variables": r[6], "subject": r[7], "body_a": r[8], "body_b": r[9],
               "words_a": len(r[8].split()), "words_b": len(r[9].split())} for r in rows]
    summary = {
        "model": model, "leads_attempted": leads, "emails_stored": len(emails), "dead_letter": dead,
        "first_try_valid": ok, "valid_after_repair": repaired, "rejected_answers": invalid, "request_errors": errors,
        "requests": requests, "prompt_tokens": pt, "completion_tokens": ct,
        "cost_usd": round(cost, 6), "cost_per_1000_emails": round(cost / max(len(emails), 1) * 1000, 4),
        "avg_latency_ms": latency,
        "avg_words": round(sum(e["words_a"] + e["words_b"] for e in emails) / max(2 * len(emails), 1), 1),
        "rejection_reasons": dict(reasons.most_common()),
    }
    summary["results_file"] = _shown(_save(f"copy-{model.split('/')[-1]}.json",
                                           {"summary": summary, "emails": emails}, results_dir))
    return summary


def eval_scoring(llm: LLMClient, model: str, world: dict, n: int = 60, seed: int = 7,
                 pause_s: float = 0.0) -> dict:
    """LLM score vs the rubric on a sample of the synthetic world's people."""
    companies = {c["domain"]: c for c in world["companies"]}
    people = random.Random(seed).sample(world["people"], k=min(n, len(world["people"])))
    log: list[LLMCall] = []
    items = []
    for i, p in enumerate(people):
        c = companies[p["company_domain"]]
        lead = {"id": f"eval-{i}", "title": p["title"], "seniority": p["seniority"], "industry": c["industry"],
                "size_band": c["size_band"], "country": c["country"]}
        rubric, _ = rubric_score(lead)
        try:
            got, why = llm_score(llm, model, lead, log)
        except (LLMError, LLMInvalid) as e:
            got, why = None, f"error: {e}"[:200]
        items.append({**lead, "rubric": rubric, "llm": got, "rationale": why})
        if pause_s:
            time.sleep(pause_s)
    scored = [i for i in items if i["llm"] is not None]
    summary = {
        "model": model, "n": len(items), "errors": len(items) - len(scored),
        "exact_agreement": round(sum(i["llm"] == i["rubric"] for i in scored) / max(len(scored), 1), 3),
        "within_one": round(sum(abs(i["llm"] - i["rubric"]) <= 1 for i in scored) / max(len(scored), 1), 3),
        "eligibility_agreement": round(sum((i["llm"] >= 3) == (i["rubric"] >= 3) for i in scored) / max(len(scored), 1), 3),
        "rubric_distribution": dict(sorted(Counter(i["rubric"] for i in items).items())),
        "cost_usd": round(sum(c.cost_usd for c in log), 6),
        "cost_per_1000_leads": round(sum(c.cost_usd for c in log) / max(len(items), 1) * 1000, 4),
        "avg_latency_ms": round(sum(c.latency_ms for c in log) / max(len(log), 1)),
        "disagreements": [{k: i[k] for k in ("title", "seniority", "industry", "size_band", "country", "rubric", "llm",
                                             "rationale")} for i in scored if i["llm"] != i["rubric"]][:15],
    }
    summary["results_file"] = str(_save(f"scoring-{model.split('/')[-1]}.json",
                                        {"summary": summary, "items": items}).relative_to(ROOT))
    return summary
