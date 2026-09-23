"""A stand-in for the sending provider: accepts queued emails, emits signed events.

Each theory has a hidden true positive-reply rate the pipeline does not know; the
kill switch has to find the bad one from the events alone. Reply texts come from
their own bank, not from eval/replies.jsonl, so a simulation run is a second,
independent check of the reply classifier: the simulator knows which class it
meant to send.

Events travel as signed JSON bytes through `webhook.handle`, the same path a real
provider's webhook would take.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from leadengine.sending import Queued
from leadengine.webhook import sign

# Hidden truth per theory name: positive reply rate. Other outcomes are shared.
DEFAULT_TRUE_RATES = {
    "fresh funding, hiring for pipeline": 0.030,
    "new tool, broken attribution": 0.012,
    "new office, new market": 0.003,
}
SHARED = {"bounce": 0.015, "unsubscribe": 0.008, "negative": 0.020, "ooo": 0.030, "neutral": 0.010,
          "referral": 0.005, "open": 0.45}

REPLY_BANK = {
    "positive": [
        "Good timing, {first}. Can you walk me through how the attribution model works? Free Tuesday or Wednesday.",
        "Yes, this is exactly what our board keeps asking about. What does onboarding look like?",
        "Interested. Send over pricing and a short case study please.",
        "Let's talk. I can do 30 minutes next week, pick a slot.",
        "We're rebuilding reporting right now, so happy to take a look. How long does setup take?",
    ],
    "negative": [
        "Thanks, but we handle attribution in-house and it works well enough.",
        "Not a priority for us this year. No thanks.",
        "We're locked into a contract with another vendor until 2028, so it's a no.",
        "Appreciate it, but this doesn't fit how we sell.",
    ],
    "ooo": [
        "I'm out of office until the 14th with no access to email.",
        "Automatic reply: on annual leave, back next Monday.",
        "Thanks for your message. I'm away at a conference and will reply after I return.",
    ],
    "neutral": [
        "Busy quarter, maybe later.",
        "Thanks for the note.",
        "Can't look at new tools right now. Maybe after the reorg.",
    ],
    "referral": [
        "I'm not the right person. Please talk to our head of marketing ops, sam.ortiz@{domain}.",
        "Forwarding this to Priya who runs our analytics, she'll decide.",
    ],
    "unsubscribe": [
        "Take this address off your mailing, thanks.",
        "No more emails from Tallybird please. Unsubscribe me.",
    ],
}


@dataclass
class _Pending:
    due_day: int
    event: dict
    intended: str | None = None


@dataclass
class Simulator:
    secret: str
    true_rates: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TRUE_RATES))
    seed: int = 0
    start: datetime = datetime(2026, 9, 1, 9, 0)
    real_time: bool = False  # scheduled runs stamp events with the actual time
    pending: list[_Pending] = field(default_factory=list)
    intended_class: dict[str, str] = field(default_factory=dict)  # event id -> class the simulator meant

    def _rng(self, key: str) -> random.Random:
        return random.Random(int(hashlib.sha1(f"{self.seed}|{key}".encode()).hexdigest()[:16], 16))

    def accept(self, queued: list[Queued], day: int) -> None:
        """Schedule every event this batch will produce.

        Seeded by the recipient's email, which the world fixes, not by the lead's
        UUID, which is new in every database: two runs that differ only in the pause
        rule then see the same person answer the same way (a paired comparison).
        """
        for q in queued:
            rng = self._rng(f"{q.email}|{q.theory_name}")
            domain = q.email.split("@", 1)[1]
            base = {"lead_email": q.email, "campaign_id": q.campaign_external_id, "provider": "simulator"}
            self._add(day, {**base, "id": f"{q.lead_id}:sent", "type": "sent"})
            r = rng.random()
            if r < SHARED["bounce"]:
                self._add(day, {**base, "id": f"{q.lead_id}:bounce", "type": "bounce"})
                continue
            if rng.random() < SHARED["open"]:
                self._add(day + rng.randint(0, 2), {**base, "id": f"{q.lead_id}:open", "type": "open"})
            outcomes = [("positive", self.true_rates.get(q.theory_name, 0.01)),
                        *((k, SHARED[k]) for k in ("negative", "ooo", "neutral", "referral"))]
            r = rng.random()
            if r < SHARED["unsubscribe"]:
                self._add(day + 1, {**base, "id": f"{q.lead_id}:unsub", "type": "unsubscribe"})
                continue
            r -= SHARED["unsubscribe"]
            for cls, p in outcomes:
                if r < p:
                    text = rng.choice(REPLY_BANK[cls]).format(first=q.first_name or "there", domain=domain)
                    ev_id = f"{q.lead_id}:reply"
                    self._add(day + rng.randint(1, 4), {**base, "id": ev_id, "type": "reply", "text": text}, cls)
                    self.intended_class[ev_id] = cls
                    break
                r -= p

    def _add(self, day: int, event: dict, intended: str | None = None) -> None:
        # Bounded: at most a handful of events per lead, and the caller drains them daily.
        self.pending.append(_Pending(day, event, intended))

    def deliver(self, day: int) -> list[tuple[str, bytes]]:
        """Signed webhook requests due on `day`: (X-Signature header, raw body)."""
        due = [p for p in self.pending if p.due_day <= day]
        self.pending = [p for p in self.pending if p.due_day > day]
        out = []
        ts = datetime.now(timezone.utc).replace(tzinfo=None) if self.real_time else self.start + timedelta(days=day)
        for p in due:
            body = json.dumps({**p.event, "occurred_at": ts.isoformat() + "Z"}).encode()
            out.append((sign(self.secret, body), body))
        return out
