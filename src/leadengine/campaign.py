"""Run days of outreach against the simulator: queue, deliver events, judge theories.

One simulated day:
  1. queue sendable leads within mailbox capacity and hand them to the simulator;
  2. deliver the events due today as signed webhooks through `webhook.handle`;
  3. run the kill switch; leads of a paused theory go back to theory assignment
     and get new emails under the best remaining theory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import psycopg

from leadengine.drafting import assign, draft_copy
from leadengine.killswitch import UPPER_RULE, Rule, TheoryHealth, evaluate_theories
from leadengine.llm import LLMCall, LLMClient, LLMError, LLMInvalid
from leadengine.replies import classify_keywords, classify_llm
from leadengine.sending import ensure_mailboxes, queue_for_sending
from leadengine.simulator import Simulator
from leadengine.webhook import Classifier, handle


def keyword_classifier() -> Classifier:
    return lambda text: (classify_keywords(text)[0], "keywords")


def llm_classifier(llm: LLMClient, model: str, log: list[LLMCall]) -> Classifier:
    def classify(text: str) -> tuple[str, str]:
        try:
            return classify_llm(llm, model, text, log).category, "llm"
        except (LLMError, LLMInvalid):
            # The event must not be lost because the model is down; the fallback is marked.
            return classify_keywords(text)[0], "keywords_fallback"
    return classify


@dataclass
class DayLog:
    day: int
    queued: int
    events: int
    rejected: int
    duplicates: int
    paused: list[str]
    released: int


@dataclass
class SimResult:
    days: list[DayLog] = field(default_factory=list)
    health: list[TheoryHealth] = field(default_factory=list)
    paused_on_day: dict[str, int] = field(default_factory=dict)


def drain_copy(conn: psycopg.Connection, writer: LLMClient, model: str, max_rounds: int = 500) -> int:
    total = 0
    for _ in range(max_rounds):
        n = draft_copy(conn, writer, model=model, batch=200)["copy_ready"]
        total += n
        if n == 0:
            return total
    raise RuntimeError("copy drafting did not drain")


def run_simulation(conn: psycopg.Connection, sim: Simulator, classify: Classifier, *, days: int, writer: LLMClient,
                   writer_model: str = "template", rule: Rule = UPPER_RULE, mailboxes: int = 5,
                   daily_limit: int = 30) -> SimResult:
    ensure_mailboxes(conn, mailboxes, daily_limit)
    res = SimResult()
    for day in range(days):
        now = sim.start + timedelta(days=day)
        queued = queue_for_sending(conn, now=now)
        sim.accept(queued, day)
        rejected = dup = 0
        requests = sim.deliver(day)
        for header, body in requests:
            status, out = handle(conn, sim.secret, header, body, classify)
            rejected += status != 200
            dup += bool(out.get("duplicate"))
        health = evaluate_theories(conn, rule)
        paused = [h.name for h in health if h.decision == "pause"]
        for name in paused:
            res.paused_on_day.setdefault(name, day)
        released = 0
        if paused:
            released = conn.execute(
                "SELECT count(*) FROM leads WHERE status = 'scored' AND theory_id IS NULL").fetchone()[0]
            conn.commit()
            assign(conn)
            drain_copy(conn, writer, writer_model)
        res.days.append(DayLog(day, len(queued), len(requests), rejected, dup, paused, released))
        res.health = health
    return res
