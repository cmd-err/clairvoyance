-- 087: merchants.max_concurrent_calls — how many calls a merchant may hold
-- at once on Buddy's per-number dialler (its contracted concurrency). NULL =
-- no cap (today's behaviour). Read by the dialler's fill (a soft cap, see
-- dispatch/dialq.py); a provider-agnostic CRM can read the same column.
-- At least 1: a cap of 0 would park the merchant's leads with no alert.
-- Set by SQL for now.
ALTER TABLE merchants ADD COLUMN IF NOT EXISTS max_concurrent_calls integer
    CHECK (max_concurrent_calls IS NULL OR max_concurrent_calls > 0);
