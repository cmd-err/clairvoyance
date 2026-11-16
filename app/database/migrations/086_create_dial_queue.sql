-- 086: dial_queue — one row per outbound lead that is waiting for a line or
-- holds one. Deleted when the lead finishes, so it holds the open backlog
-- (future leads included), never the lead history. The dialler gives lines
-- out per number from here instead of from a global Redis list.
--
-- No foreign key to lead_call_tracker (98 GB; its writes stay untouched) and
-- no new index on it. Owner: breeze_buddy dispatch.

CREATE TABLE IF NOT EXISTS dial_queue (
    lead_id     varchar(255) PRIMARY KEY,
    -- fairness key: the lead's merchant, else its reseller
    merchant_id varchar(255) NOT NULL,
    template_id uuid         NOT NULL,
    due_at      timestamptz  NOT NULL,
    state       text         NOT NULL DEFAULT 'waiting'
                CHECK (state IN ('waiting', 'granted', 'dialing', 'live')),
    -- the number whose line this lead holds; NULL while waiting
    number_id   varchar(255),
    -- set when a line is granted: only its holder may use or return the line
    ticket      uuid,
    -- when the row entered its current state (repair reads it)
    state_at    timestamptz  NOT NULL DEFAULT now(),
    -- a cancel that came after the line was granted: the lead is not dialled,
    -- and if no call happened give-back finishes it as ABORT with this reason
    cancel_reason text,
    CHECK ((state = 'waiting') = (number_id IS NULL))
) WITH (
    -- small, high-churn table: vacuum on a fixed row count, not a fraction
    autovacuum_vacuum_scale_factor = 0,
    autovacuum_vacuum_threshold = 2000,
    autovacuum_analyze_scale_factor = 0,
    autovacuum_analyze_threshold = 2000
);

-- which templates have due waiting leads, oldest first (the tick and fill)
CREATE INDEX IF NOT EXISTS dial_queue_waiting_ix
    ON dial_queue (template_id, due_at) WHERE state = 'waiting';
-- lines held per number (fairness, repair)
CREATE INDEX IF NOT EXISTS dial_queue_busy_ix
    ON dial_queue (number_id) WHERE state <> 'waiting';
