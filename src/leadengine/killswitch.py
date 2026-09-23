"""Theory kill switch: pause theories that are confidently bad, and only those.

The design paused a theory when the Wilson *lower* bound of its positive reply rate
fell below 0.5% after 300 sends. A lower bound under the threshold means "we
cannot rule out that it is bad", which is true of almost every theory at 300
sends: a theory that really gets 1% positive replies, twice the threshold, has 3
positives in 300 on average, a lower bound of 0.34%, and is paused. The Monte Carlo
in `simulate_rule` measures how often that happens.

Here a theory is paused when the *upper* bound is below the threshold: we are
95% sure its rate is lower than the target. That never pauses a good theory by
design, and pays for it with more sends before a bad one is stopped. Both rules
are in the README table.

Guards that the design had only in the sender call, after its SQL had already
paused the theory, live here: the last active theory is never paused.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

import psycopg

from leadengine.db import require_idle

Z95 = 1.96


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n trials."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    denom = 1 + z * z / n
    return max(0.0, (centre - margin) / denom), min(1.0, (centre + margin) / denom)


@dataclass(frozen=True)
class Rule:
    name: str
    bound: str          # "lower" (the design) or "upper"
    threshold: float    # positive reply rate
    min_n: int

    def should_pause(self, k: int, n: int) -> bool:
        if n < self.min_n:
            return False
        lo, hi = wilson(k, n)
        return (lo if self.bound == "lower" else hi) < self.threshold


DESIGN_RULE = Rule("design: lower bound < 0.5% at n >= 300", "lower", 0.005, 300)
UPPER_RULE = Rule("upper bound < 1% at n >= 100", "upper", 0.01, 100)


@dataclass
class TheoryHealth:
    theory_id: str
    name: str
    sent: int
    replied: int
    positive: int
    wilson_lower: float
    wilson_upper: float
    decision: str  # keep | pause | kept_last_active | too_few


def evaluate_theories(conn: psycopg.Connection, rule: Rule = UPPER_RULE) -> list[TheoryHealth]:
    """Update theory stats, pause the confidently bad ones, release their unsent leads."""
    require_idle(conn, "evaluate_theories")
    out: list[TheoryHealth] = []
    with conn.transaction():
        rows = conn.execute(
            """SELECT s.theory_id, s.name, s.sent, s.replied, s.positive
                 FROM theory_stats s JOIN theories t ON t.id = s.theory_id
                WHERE t.status = 'active'
                ORDER BY s.name
                  FOR UPDATE OF t"""
        ).fetchall()
        for tid, name, sent, replied, positive in rows:
            lo, hi = wilson(positive, sent)
            if sent < rule.min_n:
                decision = "too_few"
            else:
                decision = "pause" if rule.should_pause(positive, sent) else "keep"
            out.append(TheoryHealth(str(tid), name, sent, replied, positive, lo, hi, decision))

        # Never leave zero active theories: keep the best of the doomed ones running.
        if out and all(h.decision == "pause" for h in out):
            best = max(out, key=lambda h: (h.wilson_lower, h.positive))
            best.decision = "kept_last_active"

        for h in out:
            conn.execute(
                """UPDATE theories
                      SET sample_size = %s,
                          reply_rate = %s,
                          positive_reply_rate = %s,
                          wilson_lower = %s,
                          status = CASE WHEN %s THEN 'paused' ELSE status END,
                          paused_at = CASE WHEN %s THEN now() ELSE paused_at END
                    WHERE id = %s""",
                (h.sent, round(100 * h.replied / h.sent, 2) if h.sent else None,
                 round(100 * h.positive / h.sent, 2) if h.sent else None, round(h.wilson_lower, 5),
                 h.decision == "pause", h.decision == "pause", h.theory_id),
            )
            if h.decision == "pause":
                conn.execute("SELECT release_theory_leads(%s)", (h.theory_id,))
    return out


def simulate_rule(rule: Rule, true_rate: float, *, runs: int = 2000, daily: int = 50, max_sent: int = 2000,
                  seed: int = 0) -> dict:
    """Monte Carlo: send `daily` emails a day, check the rule every day, up to `max_sent`.

    Returns the share of runs in which the theory was paused, and the median number
    of sends at the moment it was paused.
    """
    rng = random.Random(f"{seed}|{rule.name}|{true_rate}")
    paused_at: list[int] = []
    for _ in range(runs):
        k = n = 0
        while n < max_sent:
            for _ in range(daily):
                n += 1
                k += rng.random() < true_rate
            if rule.should_pause(k, n):
                paused_at.append(n)
                break
    paused_at.sort()
    return {
        "rule": rule.name, "true_rate": true_rate, "runs": runs,
        "paused_share": round(len(paused_at) / runs, 3),
        "median_sends_to_pause": paused_at[len(paused_at) // 2] if paused_at else None,
    }
