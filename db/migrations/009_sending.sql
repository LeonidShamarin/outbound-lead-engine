-- 009_sending.sql
-- Stage 4: queueing for the (simulated) sender, event ingest, theory health.

-- Which variant went out and through which mailbox. The design kept the variant
-- only in the sender's custom variables, so per-variant stats needed the sender.
ALTER TABLE leads ADD COLUMN IF NOT EXISTS variant    TEXT CHECK (variant IN ('A','B'));
ALTER TABLE leads ADD COLUMN IF NOT EXISTS mailbox_id UUID REFERENCES mailboxes(id) ON DELETE SET NULL;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS queued_at  TIMESTAMPTZ;
ALTER TABLE outgoing_messages ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ;

-- One live campaign per theory and provider.
CREATE UNIQUE INDEX IF NOT EXISTS uq_campaigns_theory_live
  ON campaigns(theory_id, provider) WHERE status = 'active';

-- Rejected webhooks are recorded without their body: a forged request is evidence,
-- not data, and storing attacker-controlled payloads buys nothing.
CREATE TABLE IF NOT EXISTS webhook_rejections (
  id          BIGSERIAL PRIMARY KEY,
  reason      TEXT NOT NULL,
  body_sha256 TEXT,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-theory outcomes, counted per lead, not per event, and attributed through the
-- campaign the email went out in, so moving an unsent lead to another theory never
-- rewrites history. Differences from the design's query:
--   * an out-of-office auto-reply is not a reply;
--   * a lead that replied twice counts once;
--   * an unsubscribe inside a reply counts as an unsubscribe.
CREATE OR REPLACE VIEW theory_stats AS
WITH per_lead AS (
  SELECT e.lead_id, c.theory_id,
         bool_or(e.type = 'sent')                                           AS sent,
         bool_or(e.type = 'reply' AND e.reply_class IS DISTINCT FROM 'ooo'
                 AND e.reply_class IS DISTINCT FROM 'unsubscribe')          AS replied,
         bool_or(e.reply_class = 'positive')                                AS positive,
         bool_or(e.type = 'bounce')                                         AS bounced,
         bool_or(e.type = 'unsubscribe' OR e.reply_class = 'unsubscribe')   AS unsubscribed
    FROM events e
    JOIN campaigns c ON c.id = e.campaign_id
   GROUP BY e.lead_id, c.theory_id
)
SELECT t.id AS theory_id, t.name, t.status,
       count(p.lead_id) FILTER (WHERE p.sent)         AS sent,
       count(p.lead_id) FILTER (WHERE p.replied)      AS replied,
       count(p.lead_id) FILTER (WHERE p.positive)     AS positive,
       count(p.lead_id) FILTER (WHERE p.bounced)      AS bounced,
       count(p.lead_id) FILTER (WHERE p.unsubscribed) AS unsubscribed
  FROM theories t
  LEFT JOIN per_lead p ON p.theory_id = t.id
 GROUP BY t.id, t.name, t.status;

-- Pausing a theory hands its leads back to theory assignment. Only leads that have
-- not been handed to the sender move: the design also moved 'queued' leads, which
-- are already in the sender's campaign and would get a second, different email.
CREATE OR REPLACE FUNCTION release_theory_leads(p_theory_id UUID)
RETURNS INT AS $$
DECLARE
  v_n INT;
BEGIN
  DELETE FROM outgoing_messages m
   USING leads l
   WHERE m.lead_id = l.id AND l.theory_id = p_theory_id
     AND l.status IN ('theory_assigned','variables_ready','drafting','copy_ready');
  DELETE FROM variables v
   USING leads l
   WHERE v.lead_id = l.id AND v.theory_id = p_theory_id
     AND l.status IN ('theory_assigned','variables_ready','drafting','copy_ready');
  UPDATE leads
     SET theory_id = NULL, status = 'scored', attempts = 0, last_error = NULL
   WHERE theory_id = p_theory_id
     AND status IN ('theory_assigned','variables_ready','drafting','copy_ready');
  GET DIAGNOSTICS v_n = ROW_COUNT;
  RETURN v_n;
END;
$$ LANGUAGE plpgsql;
