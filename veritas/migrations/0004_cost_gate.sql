-- Veritas phase 0.6 — cost gate + operations (architecture §11.3, §13 Q2).
-- Adds the state + columns + audit tables the hard cost gate needs:
--   * audit_runs.status gains 'awaiting_owner_approval' (a run queued because the
--     monthly aggregate cap was reached must wait for the owner review queue).
--   * audit_runs.approved_via_gate — owner granted a cap-queued run to proceed.
--   * audit_runs.gate_halt_reason — why a run was halted by the cost gate.
--   * owner_notifications — in-app owner alerts (gate halt, monthly 80% warning,
--     approval granted). §13 Q2/§9-style owner surface, no third-party email.
--   * cost_gate_events — append-only audit trail of every gate decision
--     (pre_run / mid_flight / monthly), so gating is itself auditable.
--
-- NOTE: no BEGIN/COMMIT here — scripts/migrate.py wraps each file in a
-- transaction. Statements are written defensively (IF NOT EXISTS / IF EXISTS)
-- so the file is harmless if re-applied against an already-migrated schema.
-- ---------------------------------------------------------------------------

-- audit_runs.status: add the cap-queued state to the state machine CHECK.
ALTER TABLE audit_runs DROP CONSTRAINT IF EXISTS audit_runs_status_check;
ALTER TABLE audit_runs ADD CONSTRAINT audit_runs_status_check CHECK (status IN (
    'uploaded','validating','storing','queued',
    'normalizing','matching','reporting','completed',
    'failed','cost_gate_halted','rejected_upload','awaiting_owner_approval'
));

-- Owner-granted cap override + why a run was gated.
ALTER TABLE audit_runs ADD COLUMN IF NOT EXISTS approved_via_gate boolean NOT NULL DEFAULT false;
ALTER TABLE audit_runs ADD COLUMN IF NOT EXISTS gate_halt_reason text;

-- ---------------------------------------------------------------------------
-- owner_notifications — in-app owner alerts (no third-party email at MVP).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS owner_notifications (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL,
    kind             text NOT NULL,      -- cost_gate_halted | monthly_warn | monthly_cap | approval_granted
    message          text NOT NULL,
    run_id           uuid,               -- optional audit run reference
    payload          jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    acknowledged_at  timestamptz
);
CREATE INDEX IF NOT EXISTS idx_owner_notifications_unack ON owner_notifications (acknowledged_at) WHERE acknowledged_at IS NULL;

-- ---------------------------------------------------------------------------
-- cost_gate_events — append-only audit trail of cost-gate decisions.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cost_gate_events (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL,
    run_id         uuid,
    kind           text NOT NULL,       -- pre_run | mid_flight | monthly
    decision       text NOT NULL,       -- pass | halt | warn | block | approved
    tokens_in      bigint,
    tokens_out     bigint,
    budget_in      bigint,
    budget_out     bigint,
    detail         text,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cost_gate_events_tenant_created ON cost_gate_events (tenant_id, created_at);
