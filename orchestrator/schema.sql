-- VERITAS agent state (PRD §3.3). Lives in the agent Postgres (port 5433) —
-- never in ERPNext's own database.
--
-- Rule 5: `commits` is the idempotency-key store. Its PRIMARY KEY is what makes
--         a double-post impossible even under a concurrent double dispatch.
-- Rule 6: `checkpoints` is written BEFORE any ERPNext side effect.

CREATE TABLE IF NOT EXISTS workflows (
    workflow_id        TEXT PRIMARY KEY,
    current_step       TEXT        NOT NULL,
    status             TEXT        NOT NULL,
    last_checkpoint_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    ground_truth_label TEXT,
    llm_calls          INT         NOT NULL DEFAULT 0,
    escalation_reason  TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS checkpoints (
    workflow_id TEXT        NOT NULL,
    step        TEXT        NOT NULL,
    status      TEXT        NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workflow_id, step)
);

-- Rule 5. One row per logical side effect, ever.
CREATE TABLE IF NOT EXISTS commits (
    idempotency_key TEXT PRIMARY KEY,
    workflow_id     TEXT        NOT NULL,
    step            TEXT        NOT NULL,
    doctype         TEXT        NOT NULL,
    doc_name        TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS traces (
    id            BIGSERIAL PRIMARY KEY,
    workflow_id   TEXT        NOT NULL,
    step          TEXT        NOT NULL,
    attempt       INT         NOT NULL,
    step_context  TEXT        NOT NULL,
    prompt        TEXT,
    prompt_hash   TEXT        NOT NULL,
    response      TEXT,
    response_hash TEXT        NOT NULL,
    model         TEXT,
    provenance    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS step_attempts (
    id            BIGSERIAL PRIMARY KEY,
    workflow_id   TEXT    NOT NULL,
    step          TEXT    NOT NULL,
    attempt       INT     NOT NULL,
    action        TEXT,
    rationale_ref BIGINT REFERENCES traces (id),
    verdict       TEXT,
    confidence    DOUBLE PRECISION,
    region        TEXT,
    committed     BOOLEAN NOT NULL DEFAULT FALSE,
    doc_name      TEXT,
    latency_ms    INT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workflow_id, step, attempt)
);

-- Phase 4 ground truth (PRD §3.3). Created now so the schema is complete;
-- populated by the fault-injection harness.
CREATE TABLE IF NOT EXISTS labels (
    workflow_id              TEXT PRIMARY KEY,
    fault_class              TEXT NOT NULL,
    expected_terminal_action TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS traces_workflow_idx       ON traces (workflow_id, step, attempt);
CREATE INDEX IF NOT EXISTS step_attempts_wf_idx      ON step_attempts (workflow_id, step);
CREATE INDEX IF NOT EXISTS commits_workflow_idx      ON commits (workflow_id, step);

-- The trace store is append-only (PRD §3.3): a decision record that can be
-- edited after the fact is not an audit trail. Raise loudly rather than
-- silently discarding the write. TRUNCATE does not fire triggers, so test
-- teardown can still reset the table.
CREATE OR REPLACE FUNCTION traces_append_only() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'traces is append-only (PRD 3.3): % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS traces_no_mutate ON traces;
CREATE TRIGGER traces_no_mutate
    BEFORE UPDATE OR DELETE ON traces
    FOR EACH ROW EXECUTE FUNCTION traces_append_only();
