"""Run days of outreach against the simulator: queue, deliver events, judge theories.

One simulated day:
  1. queue sendable leads within mailbox capacity and hand them to the simulator;
  2. deliver the events due today as signed webhooks through `webhook.handle`;
  3. run the kill switch; leads of a paused theory go back to theory assignment
     and get new emails under the best remaining theory.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx
import psycopg

from leadengine.drafting import assign, draft_copy
from leadengine.killswitch import UPPER_RULE, Rule, TheoryHealth, evaluate_theories
from leadengine.llm import LLMCall, LLMClient, LLMError, LLMInvalid
from leadengine.replies import classify_keywords, classify_llm
from leadengine.sending import ensure_mailboxes, queue_for_sending
from leadengine.simulator import Simulator, _Pending
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


@dataclass
class CycleResult:
    day: int
    drafted: int
    queued: int
    delivered: int
    statuses: dict[int, int]
    paused: list[str]


def run_scheduled_day(conn: psycopg.Connection, *, secret: str, classify: Classifier, writer: LLMClient,
                      writer_model: str, copy_limit: int, webhook_url: str | None = None,
                      rule: Rule = UPPER_RULE, mailboxes: int = 3, daily_limit: int = 20,
                      post: Callable[..., object] | None = None, now: datetime | None = None) -> CycleResult:
    """One scheduled run = one simulated day, with the simulator's promises kept in the DB.

    With `webhook_url`, events go over real HTTP to the deployed webhook, signed with
    `secret`; otherwise straight into `webhook.handle`. Outbox rows are deleted only
    after the run, so a crash re-delivers them next time and the event ids dedupe.
    """
    from leadengine.drafting import prepare
    from leadengine.seed.theories import load_theories

    load_theories(conn)
    ensure_mailboxes(conn, mailboxes, daily_limit)
    with conn.transaction():
        conn.execute("INSERT INTO sim_state (id) VALUES (1) ON CONFLICT DO NOTHING")
        day, seed = conn.execute("SELECT day, seed FROM sim_state WHERE id = 1").fetchone()
        loaded = conn.execute("SELECT id, due_day, event, intended FROM sim_outbox").fetchall()
    sim = Simulator(secret=secret, seed=seed, real_time=True)
    sim.pending = [_Pending(due, ev, intended) for _, due, ev, intended in loaded]

    drafted = prepare(conn, writer, copy_model=writer_model, copy_limit=copy_limit).copy.get("copy_ready", 0)
    # Mailbox limits count per calendar day of `now` (real time unless a test sets it).
    queued = queue_for_sending(conn, now=now)
    sim.accept(queued, day)
    statuses: dict[int, int] = {}
    requests = sim.deliver(day)
    for header, body in requests:
        if webhook_url:
            send = post or httpx.post
            status = send(webhook_url, content=body, timeout=30,
                          headers={"X-Signature": header, "Content-Type": "application/json"}).status_code
        else:
            status, _ = handle(conn, secret, header, body, classify)
        statuses[status] = statuses.get(status, 0) + 1
    health = evaluate_theories(conn, rule)
    paused = [h.name for h in health if h.decision == "pause"]
    if paused:
        assign(conn)
    with conn.transaction():
        conn.execute("DELETE FROM sim_outbox WHERE id = ANY(%s)", ([r[0] for r in loaded],))
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO sim_outbox (due_day, event, intended) VALUES (%s, %s, %s)",
                            [(p.due_day, json.dumps(p.event), p.intended) for p in sim.pending])
        conn.execute("UPDATE sim_state SET day = day + 1, updated_at = now() WHERE id = 1")
    return CycleResult(day, drafted, len(queued), len(requests), statuses, paused)


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
