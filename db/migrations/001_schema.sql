-- 001_schema.sql
-- Core tables. Differences from the original design:
--   * no psql-only `\connect`, so any migration runner can apply it;
--   * gen_random_uuid() (built into Postgres 13+) instead of the uuid-ossp extension;
--   * statuses are CHECK-constrained, so a typo fails loudly instead of creating
--     a status nothing ever reads;
--   * theories is created before leads, so the FK is inline and the file is
--     re-runnable (the original ALTER TABLE ADD CONSTRAINT failed on a second run);
--   * 'simulator' is a valid sending provider, because the demo never sends real mail.

CREATE TABLE IF NOT EXISTS companies (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  domain          TEXT NOT NULL UNIQUE CHECK (domain = lower(domain) AND domain !~ '^(https?://|www\.)'),
  name            TEXT,
  industry        TEXT,
  size_band       TEXT CHECK (size_band IN ('1-10','11-50','51-200','201-500','501-1000','1000+')),
  country         TEXT CHECK (country ~ '^[A-Z]{2}$'),
  status          TEXT NOT NULL DEFAULT 'pending_enrichment'
                  CHECK (status IN ('pending_enrichment','enriching','enriched','failed','excluded')),
  source          TEXT,
  attempts        INT  NOT NULL DEFAULT 0,
  last_error      TEXT,
  last_attempt_at TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  enriched_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS theories (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name                TEXT NOT NULL UNIQUE,
  hypothesis          TEXT NOT NULL,
  segment_criteria    JSONB NOT NULL DEFAULT '{}'::jsonb,
  required_variables  JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(required_variables) = 'array'),
  prompt_template     TEXT,
  status              TEXT NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft','active','paused','killed')),
  sample_size         INT,
  reply_rate          NUMERIC(5,2),
  positive_reply_rate NUMERIC(5,2),
  wilson_lower        NUMERIC(7,5),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  activated_at        TIMESTAMPTZ,
  paused_at           TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS leads (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id           UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  first_name           TEXT,
  last_name            TEXT,
  full_name            TEXT,
  title                TEXT,
  seniority            TEXT CHECK (seniority IN ('c_level','vp','director','manager','ic')),
  email                TEXT CHECK (email IS NULL OR email = lower(email)),
  email_verified       BOOLEAN NOT NULL DEFAULT false,
  email_verify_source  TEXT,
  enrichment_source    TEXT,
  enrichment_cost_usd  NUMERIC(10,4) NOT NULL DEFAULT 0,
  lead_score           SMALLINT CHECK (lead_score BETWEEN 1 AND 5),
  lead_score_rationale TEXT,
  theory_id            UUID REFERENCES theories(id) ON DELETE SET NULL,
  status               TEXT NOT NULL DEFAULT 'new'
                       CHECK (status IN ('new','enriched','scoring','scored','cold_reserve',
                                         'theory_assigned','variables_ready','drafting','copy_ready',
                                         'queued','sent','replied','bounced','unsubscribed','dead_letter')),
  external_ids         JSONB NOT NULL DEFAULT '{}'::jsonb,
  attempts             INT  NOT NULL DEFAULT 0,
  last_error           TEXT,
  last_attempt_at      TIMESTAMPTZ,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS variables (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id     UUID NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  theory_id   UUID NOT NULL REFERENCES theories(id) ON DELETE CASCADE,
  key         TEXT NOT NULL,
  value       TEXT,
  source      TEXT,
  cost_usd    NUMERIC(10,4) NOT NULL DEFAULT 0,
  fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (lead_id, theory_id, key)
);

CREATE TABLE IF NOT EXISTS outgoing_messages (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id     UUID NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  theory_id   UUID REFERENCES theories(id) ON DELETE SET NULL,
  variant     TEXT NOT NULL CHECK (variant IN ('A','B')),
  subject     TEXT NOT NULL,
  body        TEXT NOT NULL,
  model       TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (lead_id, variant)
);

CREATE TABLE IF NOT EXISTS campaigns (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  theory_id            UUID NOT NULL REFERENCES theories(id) ON DELETE CASCADE,
  external_campaign_id TEXT NOT NULL,
  provider             TEXT NOT NULL CHECK (provider IN ('instantly','smartlead','simulator')),
  status               TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','archived')),
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (provider, external_campaign_id)
);

CREATE TABLE IF NOT EXISTS events (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  external_id TEXT UNIQUE,  -- provider event id; duplicate webhooks hit ON CONFLICT DO NOTHING
  lead_id     UUID REFERENCES leads(id) ON DELETE CASCADE,
  campaign_id UUID REFERENCES campaigns(id) ON DELETE SET NULL,
  type        TEXT NOT NULL CHECK (type IN ('sent','open','click','reply','bounce','unsubscribe','complaint')),
  reply_class TEXT CHECK (reply_class IN ('positive','negative','ooo','neutral','unsubscribe')),
  payload     JSONB,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mailboxes (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email           TEXT NOT NULL UNIQUE CHECK (email = lower(email)),
  domain          TEXT NOT NULL,
  provider        TEXT NOT NULL CHECK (provider IN ('instantly','smartlead','simulator')),
  warmup_score    SMALLINT,
  bounce_rate     NUMERIC(5,2) NOT NULL DEFAULT 0,
  spam_rate       NUMERIC(5,2) NOT NULL DEFAULT 0,
  daily_limit     INT NOT NULL DEFAULT 30 CHECK (daily_limit BETWEEN 1 AND 50),
  status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','warming')),
  last_checked_at TIMESTAMPTZ
);

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS leads_touch_updated_at ON leads;
CREATE TRIGGER leads_touch_updated_at
  BEFORE UPDATE ON leads
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
