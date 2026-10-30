-- The production service (service/): who may call it, what they asked for, and
-- what a human decided about an escalation.

-- API clients. The key itself is never stored: only a per-key salt and the
-- SHA-256 of salt + secret. Keys are 32 random bytes, so a fast hash is enough;
-- what matters is that a database dump does not hand out working credentials.
CREATE TABLE IF NOT EXISTS api_clients (
    client_id  TEXT PRIMARY KEY CHECK (client_id ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    name       TEXT        NOT NULL,
    key_salt   TEXT        NOT NULL,
    key_hash   TEXT        NOT NULL,
    roles      TEXT[]      NOT NULL CHECK (roles <@ ARRAY['submitter','approver','viewer','admin']),
    disabled   BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per procurement request. (client_id, idempotency_key) is UNIQUE, so a
-- client that retries a submit after a lost response gets its original workflow
-- back, never a second one. body_sha256 tells a replay from a key reused for a
-- different request, which is refused.
CREATE TABLE IF NOT EXISTS workflow_requests (
    workflow_id     TEXT PRIMARY KEY,
    client_id       TEXT        NOT NULL REFERENCES api_clients (client_id),
    idempotency_key TEXT        NOT NULL,
    body_sha256     TEXT        NOT NULL,
    request         JSONB       NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (client_id, idempotency_key)
);

-- What a human decided about an escalated step. Append-only, like traces: a
-- decision that can be edited afterwards is not an audit trail.
CREATE TABLE IF NOT EXISTS human_decisions (
    id                BIGSERIAL PRIMARY KEY,
    workflow_id       TEXT        NOT NULL,
    step              TEXT        NOT NULL,
    decision          TEXT        NOT NULL CHECK (decision IN ('reject', 'approve_and_resume')),
    decided_by        TEXT        NOT NULL REFERENCES api_clients (client_id),
    reason            TEXT        NOT NULL CHECK (length(reason) >= 10),
    escalation_reason TEXT        NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS human_decisions_wf_idx ON human_decisions (workflow_id, step, id);
CREATE INDEX IF NOT EXISTS workflows_status_idx   ON workflows (status);

CREATE OR REPLACE FUNCTION append_only() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % is not permitted', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS human_decisions_no_mutate ON human_decisions;
CREATE TRIGGER human_decisions_no_mutate
    BEFORE UPDATE OR DELETE ON human_decisions
    FOR EACH ROW EXECUTE FUNCTION append_only();
