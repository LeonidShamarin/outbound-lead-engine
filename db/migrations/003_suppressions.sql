-- 003_suppressions.sql
-- Cross-campaign suppression list. Emails are stored lower-cased (CHECK), so the
-- lookup in leads_sendable is a plain equality that can use the primary key,
-- instead of LOWER() on both sides as in the design.

CREATE TABLE IF NOT EXISTS suppressions (
  email          TEXT PRIMARY KEY CHECK (email = lower(email)),
  reason         TEXT NOT NULL CHECK (reason IN ('unsubscribe','bounce','complaint','manual')),
  source_lead_id UUID REFERENCES leads(id) ON DELETE SET NULL,
  notes          TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_suppressions_reason ON suppressions(reason);

CREATE OR REPLACE VIEW leads_sendable AS
SELECT l.*
FROM leads l
WHERE l.status = 'copy_ready'
  AND l.email IS NOT NULL
  AND l.email_verified
  AND NOT EXISTS (SELECT 1 FROM suppressions s WHERE s.email = l.email);
