-- 002_indexes.sql
-- Hot-path indexes. Added compared to the design: one person per company per email.
-- Without it the enrichment cascade stores the same contact once per provider that
-- found it, and the same person gets emailed twice.

CREATE INDEX IF NOT EXISTS idx_companies_status     ON companies(status);
CREATE INDEX IF NOT EXISTS idx_companies_created_at ON companies(created_at);

CREATE INDEX IF NOT EXISTS idx_leads_status        ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_theory        ON leads(theory_id);
CREATE INDEX IF NOT EXISTS idx_leads_company       ON leads(company_id);
CREATE INDEX IF NOT EXISTS idx_leads_status_score  ON leads(status, lead_score)
  WHERE status IN ('scored','theory_assigned','variables_ready','copy_ready');

CREATE UNIQUE INDEX IF NOT EXISTS uq_leads_company_email
  ON leads(company_id, email) WHERE email IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_variables_lead_theory ON variables(lead_id, theory_id);

CREATE INDEX IF NOT EXISTS idx_events_lead        ON events(lead_id);
CREATE INDEX IF NOT EXISTS idx_events_campaign    ON events(campaign_id);
CREATE INDEX IF NOT EXISTS idx_events_type        ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_reply_class ON events(reply_class) WHERE reply_class IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_theories_status        ON theories(status);
CREATE INDEX IF NOT EXISTS idx_campaigns_theory       ON campaigns(theory_id);
CREATE INDEX IF NOT EXISTS idx_outgoing_messages_lead ON outgoing_messages(lead_id);
CREATE INDEX IF NOT EXISTS idx_mailboxes_status       ON mailboxes(status);
