-- 008_llm.sql
-- Stage 3: scoring, theories, variables, copy, reply classification.
--
-- Fixes to the design, each pinned by a test:
--   * the reply classifier returns 'referral', which events.reply_class did not
--     allow, so the first referral reply would have failed the insert;
--   * copy generation moved leads to 'generating_copy', a status the schema never
--     had; this project uses 'drafting', already in the CHECK;
--   * scoring passed leads with score >= 3, theory assignment took them, and
--     variable enrichment only picked score >= 4, so every score-3 lead got a
--     theory and then waited forever. There is one threshold now: 'scored' means
--     eligible, and every later step takes every lead in its input status;
--   * theory assignment updated leads with no lock, so two runs could assign the
--     same lead twice. It claims with SKIP LOCKED like everything else.

ALTER TABLE events DROP CONSTRAINT IF EXISTS events_reply_class_check;
ALTER TABLE events ADD CONSTRAINT events_reply_class_check
  CHECK (reply_class IN ('positive','negative','ooo','neutral','unsubscribe','referral'));

-- One row per LLM request, including rejected answers and repairs. Cost is computed
-- from the provider's reported token usage, so "cost per lead" is plain SQL.
CREATE TABLE IF NOT EXISTS llm_calls (
  id                BIGSERIAL PRIMARY KEY,
  run_id            TEXT,
  lead_id           UUID REFERENCES leads(id) ON DELETE CASCADE,
  step              TEXT NOT NULL CHECK (step IN ('score','copy','reply','theory')),
  model             TEXT NOT NULL,
  prompt_tokens     INT NOT NULL DEFAULT 0,
  completion_tokens INT NOT NULL DEFAULT 0,
  cost_usd          NUMERIC(10,6) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
  latency_ms        INT,
  outcome           TEXT NOT NULL CHECK (outcome IN ('ok','repaired','invalid','error')),
  error             TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_lead ON llm_calls(lead_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_step ON llm_calls(step, created_at);

-- Leads claimed into a working status ('scoring', 'drafting') by a run that then
-- died go back through mark_lead_failed, so the release counts as an attempt and a
-- lead that crashes the process every time ends in dead_letter, not in a loop.
CREATE OR REPLACE FUNCTION release_stale_leads(
  p_status       TEXT,
  p_back_to      TEXT,
  p_older_than   INTERVAL,
  p_max_attempts INT DEFAULT 3
) RETURNS INT AS $$
DECLARE
  v_id UUID;
  v_n  INT := 0;
BEGIN
  FOR v_id IN
    SELECT id FROM leads
     WHERE status = p_status AND last_attempt_at < now() - p_older_than
     FOR UPDATE SKIP LOCKED
  LOOP
    PERFORM mark_lead_failed(v_id, p_status, 'claim expired: the run that took it did not finish',
                             p_back_to, p_max_attempts);
    v_n := v_n + 1;
  END LOOP;
  RETURN v_n;
END;
$$ LANGUAGE plpgsql;

-- Signals (funding, hiring, tool changes, news) are fetched once per company, not
-- per lead and theory as in the design: five people at one company share one
-- funding round. value NULL means "looked, found nothing", so it is not re-fetched.
CREATE TABLE IF NOT EXISTS company_signals (
  company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  key        TEXT NOT NULL,
  value      TEXT,
  source     TEXT NOT NULL,
  cost_usd   NUMERIC(10,4) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (company_id, key)
);

-- Match scored leads to the best active theory that fits their segment AND whose
-- required variables the company actually has. The design assigned a theory first
-- and fetched its variables afterwards; a lead whose company had no funding round
-- then sat in 'theory_assigned' forever. Here a lead that no theory fits goes to
-- 'cold_reserve' with the reason, and the chosen theory's variables are copied in
-- the same statement, so mark_variables_ready() can promote it right away.
CREATE OR REPLACE FUNCTION assign_theories(p_limit INT, OUT assigned INT, OUT no_theory INT) AS $$
BEGIN
  WITH picked AS (
    SELECT l.id FROM leads l
     WHERE l.status = 'scored'
       AND EXISTS (SELECT 1 FROM company_signals s WHERE s.company_id = l.company_id)
     ORDER BY l.lead_score DESC, l.created_at, l.id
     LIMIT p_limit
     FOR UPDATE SKIP LOCKED
  ),
  best AS (
    SELECT DISTINCT ON (l.id) l.id AS lead_id, l.company_id, t.id AS theory_id, t.required_variables
      FROM leads l
      JOIN picked p ON p.id = l.id
      JOIN companies c ON c.id = l.company_id
      JOIN theories t ON t.status = 'active'
     WHERE (NOT (t.segment_criteria ? 'industry')  OR t.segment_criteria->'industry'  @> to_jsonb(c.industry))
       AND (NOT (t.segment_criteria ? 'seniority') OR t.segment_criteria->'seniority' @> to_jsonb(l.seniority))
       AND (NOT (t.segment_criteria ? 'size_band') OR t.segment_criteria->'size_band' @> to_jsonb(c.size_band))
       AND (NOT (t.segment_criteria ? 'country')   OR t.segment_criteria->'country'   @> to_jsonb(c.country))
       AND t.required_variables <@ COALESCE(
             (SELECT jsonb_agg(s.key) FROM company_signals s
               WHERE s.company_id = c.id AND s.value IS NOT NULL), '[]'::jsonb)
     -- theories seeded in one transaction share created_at; name breaks the tie,
     -- so the choice does not depend on random UUIDs
     ORDER BY l.id, t.positive_reply_rate DESC NULLS LAST, t.created_at, t.name
  ),
  vars AS (
    INSERT INTO variables (lead_id, theory_id, key, value, source, cost_usd)
    SELECT b.lead_id, b.theory_id, s.key, s.value, s.source, 0
      FROM best b
      JOIN company_signals s ON s.company_id = b.company_id AND b.required_variables ? s.key
    ON CONFLICT (lead_id, theory_id, key) DO NOTHING
    RETURNING 1
  ),
  upd AS (
    UPDATE leads l SET theory_id = b.theory_id, status = 'theory_assigned'
      FROM best b WHERE l.id = b.lead_id
    RETURNING 1
  ),
  cold AS (
    UPDATE leads l SET status = 'cold_reserve', last_error = 'no active theory fits this lead and its signals'
     WHERE l.id IN (SELECT id FROM picked) AND l.id NOT IN (SELECT lead_id FROM best)
    RETURNING 1
  )
  -- vars is not read here; a data-modifying CTE runs whether or not it is referenced.
  SELECT (SELECT count(*) FROM upd), (SELECT count(*) FROM cold)
    INTO assigned, no_theory;
END;
$$ LANGUAGE plpgsql;
