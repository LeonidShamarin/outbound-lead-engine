"""Signed event webhook: verify, dedupe, apply.

Signature header: `X-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256>` over
`f"{t}.".encode() + raw_body`. Differences from the design's verifier:

* it signed `JSON.stringify(parsed_body)`, not the bytes received, so any change
  in key order or whitespace between sender and receiver broke valid webhooks;
* its error message was `Invalid HMAC signature, got X, expected Y`: whoever sent
  a forged request got the correct signature for it back, and could replay it;
* nothing bounded the age of a request, so a captured webhook stayed valid
  forever. The timestamp is inside the signed bytes and must be within 5 minutes;
  inside that window, duplicates are dropped by `events.external_id`.

A rejection says only which check failed. It is recorded with the body's hash.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

import psycopg

TOLERANCE_S = 300
MAX_BODY_BYTES = 64 * 1024
EVENT_TYPES = ("sent", "open", "click", "reply", "bounce", "unsubscribe", "complaint")


class WebhookRejected(Exception):
    def __init__(self, reason: str, status: int = 401) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def sign(secret: str, body: bytes, t: int | None = None) -> str:
    t = int(time.time()) if t is None else t
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verify(secret: str, header: str | None, body: bytes, now: float | None = None) -> None:
    if not secret:
        raise WebhookRejected("server has no webhook secret configured", 500)
    if len(body) > MAX_BODY_BYTES:
        raise WebhookRejected("body too large", 413)
    if not header:
        raise WebhookRejected("missing signature")
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        t = int(parts["t"])
        given = parts["v1"]
    except (ValueError, KeyError):
        raise WebhookRejected("malformed signature header") from None
    now = time.time() if now is None else now
    if abs(now - t) > TOLERANCE_S:
        raise WebhookRejected("stale or future timestamp")
    expected = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, given):
        raise WebhookRejected("bad signature")


@dataclass
class Applied:
    duplicate: bool
    event_type: str
    lead_status: str | None = None
    reply_class: str | None = None
    classifier: str | None = None


# (text) -> (category, how it was classified)
Classifier = Callable[[str], tuple[str, str]]


def apply_event(conn: psycopg.Connection, event: dict, classify: Classifier) -> Applied:
    """Store one verified event and move the lead. Idempotent by event id."""
    etype = event.get("type")
    if etype not in EVENT_TYPES:
        raise WebhookRejected(f"unknown event type {etype!r}", 422)
    ext_id, email = event.get("id"), (event.get("lead_email") or "").strip().lower()
    if not ext_id or not email:
        raise WebhookRejected("event needs id and lead_email", 422)

    with conn.transaction():
        row = conn.execute(
            """SELECT l.id, l.status, c.id FROM leads l
                 LEFT JOIN campaigns c ON c.external_campaign_id = %s AND c.provider = %s
                WHERE l.email = %s
                ORDER BY l.queued_at DESC NULLS LAST LIMIT 1""",
            (event.get("campaign_id"), event.get("provider", "simulator"), email),
        ).fetchone()
        if row is None:
            raise WebhookRejected("unknown lead", 422)
        lead_id, status, campaign_id = row
        reply_class = how = None
        if etype == "reply":
            reply_class, how = classify(event.get("text") or "")
        inserted = conn.execute(
            """INSERT INTO events (external_id, lead_id, campaign_id, type, reply_class, payload, occurred_at)
               VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s::timestamptz, now()))
               ON CONFLICT (external_id) DO NOTHING RETURNING id""",
            (ext_id, lead_id, campaign_id, etype, reply_class,
             json.dumps({**event, "classifier": how}), event.get("occurred_at")),
        ).fetchone()
        if inserted is None:
            return Applied(duplicate=True, event_type=etype)

        new_status = None
        if etype == "sent" and status == "queued":
            new_status = "sent"
            conn.execute("""UPDATE outgoing_messages m SET sent_at = now() FROM leads l
                             WHERE m.lead_id = l.id AND l.id = %s AND m.variant = l.variant""", (lead_id,))
        elif etype == "bounce":
            new_status = "bounced"
            _suppress(conn, email, "bounce", lead_id)
        elif etype in ("unsubscribe", "complaint") or reply_class == "unsubscribe":
            new_status = "unsubscribed"
            _suppress(conn, email, "complaint" if etype == "complaint" else "unsubscribe", lead_id)
        elif etype == "reply" and reply_class != "ooo" and status in ("queued", "sent"):
            # An out-of-office is not an answer: the lead stays 'sent'. A reply after
            # an unsubscribe or a bounce must not undo them.
            new_status = "replied"
        if new_status:
            conn.execute("UPDATE leads SET status = %s, email_verified = email_verified AND %s WHERE id = %s",
                         (new_status, new_status != "bounced", lead_id))
    return Applied(False, etype, new_status, reply_class, how)


def _suppress(conn: psycopg.Connection, email: str, reason: str, lead_id) -> None:
    conn.execute(
        """INSERT INTO suppressions (email, reason, source_lead_id) VALUES (%s, %s, %s)
           ON CONFLICT (email) DO NOTHING""", (email, reason, lead_id))


def handle(conn: psycopg.Connection, secret: str, header: str | None, body: bytes, classify: Classifier,
           now: float | None = None) -> tuple[int, dict]:
    """The whole request: verify, parse, apply. Returns (HTTP status, JSON body)."""
    try:
        verify(secret, header, body, now)
        try:
            event = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise WebhookRejected("body is not JSON", 400) from None
        if not isinstance(event, dict):
            raise WebhookRejected("body is not a JSON object", 400)
        r = apply_event(conn, event, classify)
    except WebhookRejected as e:
        with conn.transaction():
            conn.execute("INSERT INTO webhook_rejections (reason, body_sha256) VALUES (%s, %s)",
                         (e.reason, hashlib.sha256(body).hexdigest()))
        return e.status, {"error": e.reason}
    return 200, {"ok": True, "duplicate": r.duplicate}
