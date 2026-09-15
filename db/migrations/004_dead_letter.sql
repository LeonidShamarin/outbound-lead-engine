-- 004_dead_letter.sql
-- Dead letter + retry bookkeeping.
-- Fix compared to the design: mark_lead_failed inserted a new dead_letter row on
-- every call once attempts reached the maximum, so a lead that kept failing piled
-- up duplicates. Now a lead moves to dead_letter exactly once.

CREATE TABLE IF NOT EXISTS dead_letter (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_type   TEXT NOT NULL CHECK (entity_type IN ('lead','company','variable_fetch')),
  entity_id     UUID NOT NULL,
  step          TEXT NOT NULL,
  error_message TEXT,
  payload       JSONB,
  attempts      INT NOT NULL DEFAULT 0,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (entity_type, entity_id, step)
);

CREATE INDEX IF NOT EXISTS idx_dead_letter_step ON dead_letter(step);

-- Records a failure. Returns true when this call moved the lead to dead_letter.
-- p_retry_status is where the lead goes back to while attempts remain; the caller
-- passes it explicitly, because blindly returning to 'new' is how the design's
-- warning about an endless retry loop comes true.
CREATE OR REPLACE FUNCTION mark_lead_failed(
  p_lead_id      UUID,
  p_step         TEXT,
  p_error        TEXT,
  p_retry_status TEXT,
  p_max_attempts INT DEFAULT 3
) RETURNS BOOLEAN AS $$
DECLARE
  v_attempts INT;
BEGIN
  UPDATE leads
     SET attempts        = attempts + 1,
         last_error      = p_error,
         last_attempt_at = now(),
         status          = CASE WHEN attempts + 1 >= p_max_attempts THEN 'dead_letter' ELSE p_retry_status END
   WHERE id = p_lead_id
     AND status <> 'dead_letter'
  RETURNING attempts INTO v_attempts;

  IF v_attempts IS NULL OR v_attempts < p_max_attempts THEN
    RETURN false;
  END IF;

  INSERT INTO dead_letter (entity_type, entity_id, step, error_message, attempts)
  VALUES ('lead', p_lead_id, p_step, p_error, v_attempts)
  ON CONFLICT (entity_type, entity_id, step) DO NOTHING;
  RETURN true;
END;
$$ LANGUAGE plpgsql;
