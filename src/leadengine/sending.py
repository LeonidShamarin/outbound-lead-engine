"""Hand copy-ready leads to the sender, within mailbox limits.

The sender is a simulator, so nothing here reaches a real inbox. What is real is
the bookkeeping a sender needs: one campaign per theory, a deterministic A/B
variant per lead (a retry never switches variant), and a per-mailbox daily cap
(the design mentioned 15 to 30 a day as sender settings; here it is enforced
before the lead is handed over).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

import psycopg

from leadengine.db import require_idle


def variant_for(lead_id: str) -> str:
    return "A" if int(hashlib.sha1(str(lead_id).encode()).hexdigest()[:2], 16) % 2 == 0 else "B"


def ensure_mailboxes(conn: psycopg.Connection, n: int = 5, daily_limit: int = 30) -> None:
    with conn.transaction():
        for i in range(1, n + 1):
            conn.execute(
                """INSERT INTO mailboxes (email, domain, provider, daily_limit, status)
                   VALUES (%s, 'tallybird-mail.example', 'simulator', %s, 'active') ON CONFLICT (email) DO NOTHING""",
                (f"mira{i}@tallybird-mail.example", daily_limit),
            )


@dataclass
class Queued:
    lead_id: str
    email: str
    first_name: str | None
    theory_id: str
    theory_name: str
    campaign_external_id: str
    variant: str
    subject: str
    body: str


def queue_for_sending(conn: psycopg.Connection, limit: int = 1000, now: datetime | None = None) -> list[Queued]:
    """Queue as many sendable leads as the day's free mailbox capacity allows.

    `now` lets the simulator run many days in one real day; capacity is counted per
    calendar day of `now`.
    """
    require_idle(conn, "queue_for_sending")
    out: list[Queued] = []
    with conn.transaction():
        # Lock the mailboxes first (FOR UPDATE cannot sit on a GROUP BY), so two runs
        # cannot both hand out a mailbox's last free slots.
        conn.execute("SELECT id FROM mailboxes WHERE status = 'active' ORDER BY email FOR UPDATE").fetchall()
        boxes = conn.execute(
            """SELECT m.id, m.daily_limit - count(l.id) AS free
                 FROM mailboxes m
                 LEFT JOIN leads l ON l.mailbox_id = m.id
                      AND l.queued_at >= date_trunc('day', COALESCE(%(now)s::timestamptz, now()))
                      AND l.queued_at <  date_trunc('day', COALESCE(%(now)s::timestamptz, now())) + interval '1 day'
                WHERE m.status = 'active'
                GROUP BY m.id, m.email ORDER BY m.email""", {"now": now}).fetchall()
        slots = [box for box, free in boxes for _ in range(max(free, 0))]
        if not slots:
            return out
        leads = conn.execute(
            """SELECT l.id, l.email, l.first_name, l.theory_id, t.name
                 FROM leads_sendable l JOIN theories t ON t.id = l.theory_id
                WHERE t.status = 'active'
                ORDER BY l.lead_score DESC, l.created_at, l.id
                LIMIT %s
                FOR UPDATE OF l SKIP LOCKED""", (min(limit, len(slots)),)).fetchall()
        for i, (lid, email, first, tid, tname) in enumerate(leads):
            ext = f"sim-{str(tid)[:8]}"
            conn.execute(
                """INSERT INTO campaigns (theory_id, external_campaign_id, provider)
                   VALUES (%s, %s, 'simulator') ON CONFLICT DO NOTHING""", (tid, ext))
            v = variant_for(lid)
            subject, body = conn.execute(
                "SELECT subject, body FROM outgoing_messages WHERE lead_id = %s AND variant = %s", (lid, v)).fetchone()
            conn.execute(
                """UPDATE leads SET status = 'queued', variant = %s, mailbox_id = %s, queued_at = COALESCE(%s::timestamptz, now()),
                          external_ids = external_ids || jsonb_build_object('campaign', %s::text)
                    WHERE id = %s""", (v, slots[i], now, ext, lid))
            out.append(Queued(str(lid), email, first, str(tid), tname, ext, v, subject, body))
    return out
