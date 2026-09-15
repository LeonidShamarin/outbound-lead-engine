-- 005_gdpr.sql
-- Right to be forgotten: delete everything about an email and suppress it forever.

CREATE TABLE IF NOT EXISTS gdpr_deletion_log (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email           TEXT NOT NULL,
  requested_by    TEXT NOT NULL,
  reason          TEXT,
  records_deleted JSONB,
  deleted_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION gdpr_delete_by_email(
  p_email        TEXT,
  p_requested_by TEXT,
  p_reason       TEXT DEFAULT 'GDPR right to be forgotten request'
) RETURNS JSONB AS $$
DECLARE
  v_email    TEXT := lower(trim(p_email));
  v_lead_ids UUID[];
  v_n_leads  INT := 0;
  v_n_events INT := 0;
  v_n_vars   INT := 0;
  v_n_msgs   INT := 0;
BEGIN
  SELECT array_agg(id) INTO v_lead_ids FROM leads WHERE email = v_email;

  IF v_lead_ids IS NOT NULL THEN
    SELECT count(*) INTO v_n_events FROM events            WHERE lead_id = ANY(v_lead_ids);
    SELECT count(*) INTO v_n_vars   FROM variables         WHERE lead_id = ANY(v_lead_ids);
    SELECT count(*) INTO v_n_msgs   FROM outgoing_messages WHERE lead_id = ANY(v_lead_ids);
    DELETE FROM leads WHERE id = ANY(v_lead_ids);  -- events, variables, messages cascade
    GET DIAGNOSTICS v_n_leads = ROW_COUNT;
  END IF;

  -- Suppress even when nothing was found: the request must also block a future import.
  INSERT INTO suppressions (email, reason, notes)
  VALUES (v_email, 'manual', 'GDPR deletion: ' || p_reason)
  ON CONFLICT (email) DO UPDATE
    SET reason = 'manual', notes = EXCLUDED.notes, created_at = now();

  INSERT INTO gdpr_deletion_log (email, requested_by, reason, records_deleted)
  VALUES (v_email, p_requested_by, p_reason,
          jsonb_build_object('leads', v_n_leads, 'events', v_n_events,
                             'variables', v_n_vars, 'outgoing_messages', v_n_msgs));

  RETURN jsonb_build_object('found', v_lead_ids IS NOT NULL, 'email', v_email,
                            'leads_deleted', v_n_leads, 'events_deleted', v_n_events,
                            'variables_deleted', v_n_vars, 'messages_deleted', v_n_msgs,
                            'suppressed', true);
END;
$$ LANGUAGE plpgsql;
