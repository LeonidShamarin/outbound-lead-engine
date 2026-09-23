-- 007_enrichment.sql
-- Stage 2: bookkeeping for the enrichment cascade.
--
-- provider_calls is the cost ledger. Every HTTP call to a data provider lands here,
-- including the ones that failed and were not billed, so "how much does 1000 leads
-- cost" and "did two runs pay for the same company" are both plain SQL.
--
-- mark_company_failed mirrors mark_lead_failed from 004: a company that keeps
-- failing goes back to the queue a bounded number of times and then to dead_letter
-- exactly once.
--
-- release_stale_company_claims covers the case the design never handled: a run
-- that claimed companies ('enriching') and then crashed. Without it those rows stay
-- claimed forever. A released claim counts as an attempt, so a company that crashes
-- the process every time cannot loop.

CREATE TABLE IF NOT EXISTS provider_calls (
  id          BIGSERIAL PRIMARY KEY,
  run_id      TEXT,
  company_id  UUID REFERENCES companies(id) ON DELETE CASCADE,
  provider    TEXT NOT NULL CHECK (provider IN ('apollo','hunter','snov','phantombuster','findymail')),
  endpoint    TEXT NOT NULL,
  http_status INT,  -- NULL when the request never got a response (network error)
  cost_usd    NUMERIC(10,4) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
  results     INT NOT NULL DEFAULT 0,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_provider_calls_company  ON provider_calls(company_id);
CREATE INDEX IF NOT EXISTS idx_provider_calls_provider ON provider_calls(provider, created_at);

CREATE OR REPLACE FUNCTION mark_company_failed(
  p_company_id   UUID,
  p_step         TEXT,
  p_error        TEXT,
  p_max_attempts INT DEFAULT 3
) RETURNS BOOLEAN AS $$
DECLARE
  v_attempts INT;
BEGIN
  UPDATE companies
     SET attempts        = attempts + 1,
         last_error      = p_error,
         last_attempt_at = now(),
         status          = CASE WHEN attempts + 1 >= p_max_attempts THEN 'failed' ELSE 'pending_enrichment' END
   WHERE id = p_company_id
     AND status = 'enriching'
  RETURNING attempts INTO v_attempts;

  IF v_attempts IS NULL OR v_attempts < p_max_attempts THEN
    RETURN false;
  END IF;

  INSERT INTO dead_letter (entity_type, entity_id, step, error_message, attempts)
  VALUES ('company', p_company_id, p_step, p_error, v_attempts)
  ON CONFLICT (entity_type, entity_id, step) DO NOTHING;
  RETURN true;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION release_stale_company_claims(
  p_older_than   INTERVAL,
  p_max_attempts INT DEFAULT 3
) RETURNS INT AS $$
DECLARE
  v_id UUID;
  v_n  INT := 0;
BEGIN
  FOR v_id IN
    SELECT id FROM companies
     WHERE status = 'enriching'
       AND last_attempt_at < now() - p_older_than
     FOR UPDATE SKIP LOCKED
  LOOP
    PERFORM mark_company_failed(v_id, 'enrich', 'claim expired: the run that took it did not finish', p_max_attempts);
    v_n := v_n + 1;
  END LOOP;
  RETURN v_n;
END;
$$ LANGUAGE plpgsql;
