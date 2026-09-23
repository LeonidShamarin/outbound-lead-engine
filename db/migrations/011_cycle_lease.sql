-- 011_cycle_lease.sql
-- A scheduled cycle can now be started from two places (GitHub Actions and n8n
-- through /api/cycle). Two cycles at once would both read the simulator outbox and
-- both write back what they did not deliver. A session advisory lock is not an
-- option: the deployed app talks to Neon through a transaction-mode pooler, where
-- a session lock may land on a different server connection than its unlock. A
-- lease row with an expiry works through any pooler and frees itself if the
-- holder dies.

ALTER TABLE sim_state ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ;
ALTER TABLE sim_state ADD COLUMN IF NOT EXISTS lease_holder TEXT;
