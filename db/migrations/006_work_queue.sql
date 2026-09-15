-- 006_work_queue.sql
-- The two fixes from design §17.1, as functions every pipeline step calls.

-- (b) Claim a batch without racing another run. Two concurrent cycles used to pick
-- the same 50 companies and spend API credits twice. SKIP LOCKED makes the second
-- run take the next rows instead of waiting or duplicating, and the status flips
-- to 'enriching' in the same statement, so the claim survives the transaction.
CREATE OR REPLACE FUNCTION claim_companies(p_limit INT)
RETURNS SETOF companies AS $$
  WITH picked AS (
    SELECT id FROM companies
     WHERE status = 'pending_enrichment'
     ORDER BY created_at, id
     LIMIT p_limit
     FOR UPDATE SKIP LOCKED
  )
  UPDATE companies c
     SET status = 'enriching', last_attempt_at = now()
    FROM picked
   WHERE c.id = picked.id
  RETURNING c.*;
$$ LANGUAGE sql;

-- Same pattern for leads moving between any two statuses (scoring, drafting, ...).
CREATE OR REPLACE FUNCTION claim_leads(p_from TEXT, p_to TEXT, p_limit INT)
RETURNS SETOF leads AS $$
  WITH picked AS (
    SELECT id FROM leads
     WHERE status = p_from
     ORDER BY created_at, id
     LIMIT p_limit
     FOR UPDATE SKIP LOCKED
  )
  UPDATE leads l
     SET status = p_to, last_attempt_at = now()
    FROM picked
   WHERE l.id = picked.id
  RETURNING l.*;
$$ LANGUAGE sql;

-- (a) The design's aggregator used COUNT(DISTINCT (subquery)) inside HAVING, which
-- Postgres rejects, and its corrected version needed bool_and over a subquery.
-- A JSONB array of strings supports containment directly: the lead is ready when
-- its theory's required keys are a subset of the keys it already has.
CREATE OR REPLACE FUNCTION mark_variables_ready()
RETURNS INT AS $$
DECLARE
  v_n INT;
BEGIN
  UPDATE leads l
     SET status = 'variables_ready'
    FROM theories t
   WHERE l.theory_id = t.id
     AND l.status = 'theory_assigned'
     AND t.required_variables <@ COALESCE(
           (SELECT jsonb_agg(v.key) FROM variables v
             WHERE v.lead_id = l.id AND v.theory_id = t.id),
           '[]'::jsonb);
  GET DIAGNOSTICS v_n = ROW_COUNT;
  RETURN v_n;
END;
$$ LANGUAGE plpgsql;
