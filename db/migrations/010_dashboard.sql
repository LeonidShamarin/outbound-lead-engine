-- 010_dashboard.sql
-- Stage 5: read-only views for the dashboard, replacing the design's Metabase cards,
-- and the state a scheduled cycle needs to simulate one day per run.
--
-- Metabase cards not carried over: the two LinkedIn pool cards (the LinkedIn module
-- stays documentation) and "Pipeline value", which needs an average deal size the
-- data does not have; inventing one would make every number on it fiction.

CREATE OR REPLACE FUNCTION wilson_bound(k BIGINT, n BIGINT, upper BOOLEAN) RETURNS NUMERIC AS $$
  SELECT CASE WHEN n = 0 THEN NULL ELSE round((
    (k::float8 / n + 1.9208 / n
       + CASE WHEN upper THEN 1 ELSE -1 END
         * 1.96 * sqrt((k::float8 / n) * (1 - k::float8 / n) / n + 0.9604 / (n::float8 * n)))
    / (1 + 3.8416 / n))::numeric, 5) END
$$ LANGUAGE sql IMMUTABLE;

CREATE OR REPLACE VIEW v_funnel AS
SELECT
  (SELECT count(*) FROM companies)                                                AS companies,
  (SELECT count(*) FROM leads)                                                    AS contacts,
  (SELECT count(*) FROM leads WHERE email_verified)                               AS verified,
  (SELECT count(*) FROM leads WHERE lead_score >= 3)                              AS eligible,
  (SELECT count(*) FROM leads WHERE theory_id IS NOT NULL OR queued_at IS NOT NULL) AS with_theory,
  (SELECT count(DISTINCT lead_id) FROM events WHERE type = 'sent')                AS sent,
  (SELECT count(DISTINCT lead_id) FROM events WHERE type = 'reply'
      AND reply_class NOT IN ('ooo', 'unsubscribe'))                              AS replied,
  (SELECT count(DISTINCT lead_id) FROM events WHERE reply_class = 'positive')     AS positive;

CREATE OR REPLACE VIEW v_theories AS
SELECT s.theory_id, s.name, s.status, t.hypothesis, t.required_variables,
       s.sent, s.replied, s.positive, s.bounced, s.unsubscribed,
       CASE WHEN s.sent > 0 THEN round(100.0 * s.positive / s.sent, 2) END AS positive_pct,
       wilson_bound(s.positive, s.sent, false)                               AS wilson_lower,
       wilson_bound(s.positive, s.sent, true)                                AS wilson_upper,
       t.paused_at, t.created_at
  FROM theory_stats s JOIN theories t ON t.id = s.theory_id;

-- Everything the pipeline paid for, by step. Signals are per company, LLM per call.
CREATE OR REPLACE VIEW v_costs AS
SELECT 'enrichment: ' || provider AS item, sum(cost_usd) AS usd, count(*) AS calls FROM provider_calls GROUP BY provider
UNION ALL
SELECT 'signals: ' || source, sum(cost_usd), count(*) FROM company_signals GROUP BY source
UNION ALL
SELECT 'llm: ' || step || ' (' || model || ')', sum(cost_usd), count(*) FROM llm_calls GROUP BY step, model;

CREATE OR REPLACE VIEW v_mailboxes AS
SELECT m.email, m.status, m.daily_limit,
       count(l.id) FILTER (WHERE l.queued_at IS NOT NULL)          AS assigned,
       count(l.id) FILTER (WHERE l.status = 'bounced')             AS bounced,
       count(l.id) FILTER (WHERE l.status = 'unsubscribed')        AS unsubscribed,
       CASE WHEN count(l.id) > 0
            THEN round(100.0 * count(l.id) FILTER (WHERE l.status = 'bounced') / count(l.id), 2) END AS bounce_pct
  FROM mailboxes m LEFT JOIN leads l ON l.mailbox_id = m.id
 GROUP BY m.id, m.email, m.status, m.daily_limit;

-- Per day of event time: the design's "today vs yesterday" and "activity pulse".
CREATE OR REPLACE VIEW v_daily AS
SELECT date_trunc('day', occurred_at)::date AS day,
       count(*) FILTER (WHERE type = 'sent')                      AS sent,
       count(*) FILTER (WHERE type = 'open')                      AS opens,
       count(*) FILTER (WHERE type = 'reply' AND reply_class NOT IN ('ooo', 'unsubscribe')) AS replies,
       count(*) FILTER (WHERE reply_class = 'positive')           AS positive,
       count(*) FILTER (WHERE type = 'bounce')                    AS bounces,
       count(*) FILTER (WHERE type = 'unsubscribe' OR reply_class = 'unsubscribe') AS unsubscribes
  FROM events GROUP BY 1;

CREATE OR REPLACE VIEW v_positive_feed AS
SELECT e.occurred_at, l.first_name, l.title, c.name AS company, t.name AS theory, l.variant,
       e.payload->>'text' AS reply_text
  FROM events e
  JOIN leads l ON l.id = e.lead_id
  JOIN companies c ON c.id = l.company_id
  LEFT JOIN campaigns ca ON ca.id = e.campaign_id
  LEFT JOIN theories t ON t.id = ca.theory_id
 WHERE e.reply_class = 'positive';

CREATE OR REPLACE VIEW v_reply_classes AS
SELECT reply_class, count(*) AS replies,
       count(*) FILTER (WHERE payload->>'classifier' = 'llm')                AS by_llm,
       count(*) FILTER (WHERE payload->>'classifier' = 'keywords_fallback')  AS by_fallback
  FROM events WHERE type = 'reply' GROUP BY reply_class;

CREATE OR REPLACE VIEW v_errors AS
SELECT 'dead letter: ' || entity_type || ' / ' || step AS source, count(*) AS n, max(created_at) AS last_at
  FROM dead_letter GROUP BY entity_type, step
UNION ALL
SELECT 'webhook rejected: ' || reason, count(*), max(received_at) FROM webhook_rejections GROUP BY reason
UNION ALL
SELECT 'llm answer rejected: ' || step, count(*), max(created_at) FROM llm_calls WHERE outcome = 'invalid' GROUP BY step
UNION ALL
SELECT 'llm request failed: ' || step, count(*), max(created_at) FROM llm_calls WHERE outcome = 'error' GROUP BY step;

-- A scheduled run simulates one day. What the simulator has promised to send later
-- (replies arrive days after the email) has to survive between runs.
CREATE TABLE IF NOT EXISTS sim_state (
  id         INT PRIMARY KEY CHECK (id = 1),
  day        INT NOT NULL DEFAULT 0,
  seed       INT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sim_outbox (
  id       BIGSERIAL PRIMARY KEY,
  due_day  INT NOT NULL,
  event    JSONB NOT NULL,
  intended TEXT
);
CREATE INDEX IF NOT EXISTS idx_sim_outbox_due ON sim_outbox(due_day);
