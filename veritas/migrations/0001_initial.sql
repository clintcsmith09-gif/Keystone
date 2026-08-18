-- Veritas AI Audit Engine — Phase 0 initial schema.
-- Source of truth for state machines: sprint2-architecture.md
--   §4.2 audit_runs.status  : uploaded → validating → storing → queued →
--                             normalizing → matching → reporting → completed
--                             | failed | cost_gate_halted | rejected_upload
--   §7.2 audit_jobs         : stage + status (pending/running/succeeded/failed),
--                             attempts, lease_token (FOR UPDATE SKIP LOCKED claim)
--   §9   quotes.status      : draft → pending_owner → approved | rejected
--                             (edit returns draft → pending_owner; every action
--                             is logged in owner_actions)
-- tenant_id is on EVERY business table from day one (§10.3): one real tenant at
-- MVP, real multi-tenancy later. All ids are UUIDs generated via gen_random_uuid()
-- (core in PG 13+).
--
-- NOTE: no BEGIN/COMMIT here — scripts/migrate.py wraps each file in a transaction
-- (psycopg3 savepoint model; embedded BEGIN/COMMIT breaks it).

CREATE TABLE uploads (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL,
    filename        text NOT NULL,
    size_bytes      bigint NOT NULL CHECK (size_bytes >= 0),
    sha256          text NOT NULL,
    content_type    text NOT NULL,
    storage_key     text NOT NULL UNIQUE,          -- opaque; never the filename
    status          text NOT NULL DEFAULT 'uploaded' CHECK (status IN (
                        'uploaded','validating','storing','stored',
                        'rejected_upload','quarantined'
                    )),
    uploaded_at     timestamptz NOT NULL DEFAULT now(),
    retention_until timestamptz,                   -- §10.4: 30 days default, nightly job
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_uploads_tenant_uploaded ON uploads (tenant_id, uploaded_at);
CREATE INDEX idx_uploads_retention ON uploads (retention_until) WHERE retention_until IS NOT NULL;

-- ---------------------------------------------------------------------------
-- audit_runs — §7.4 / §4.2. One run per upload (§5: idempotent start).
-- ---------------------------------------------------------------------------
CREATE TABLE audit_runs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL,
    upload_id           uuid NOT NULL REFERENCES uploads (id),
    standard            text NOT NULL,             -- e.g. ISO-27001, PCI-DSS (§13 Q1)
    rule_set_version    int  NOT NULL DEFAULT 1,
    status              text NOT NULL DEFAULT 'queued' CHECK (status IN (
                            'uploaded','validating','storing','queued',
                            'normalizing','matching','reporting','completed',
                            'failed','cost_gate_halted','rejected_upload'
                        )),
    cost_estimate_usd   numeric(12,6),             -- §11.3 pre-run gate
    actual_tokens_in    bigint,                    -- §7.4 measured from first real audits
    actual_tokens_out   bigint,
    started_at          timestamptz,
    completed_at        timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_audit_runs_one_per_upload UNIQUE (upload_id)
);

CREATE INDEX idx_audit_runs_tenant_status ON audit_runs (tenant_id, status, created_at);

-- ---------------------------------------------------------------------------
-- audit_jobs — §7.2 Postgres-backed job queue. Workers claim ready jobs with
--   SELECT ... WHERE status IN ('pending','running') AND (leased_at IS NULL OR
--   leased_at < now() - lease_timeout) ORDER BY created_at FOR UPDATE SKIP LOCKED
-- No Redis at MVP. Unique (run_id, stage) = one job per pipeline stage per run.
-- ---------------------------------------------------------------------------
CREATE TABLE audit_jobs (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL,
    run_id       uuid NOT NULL REFERENCES audit_runs (id) ON DELETE CASCADE,
    stage        text NOT NULL CHECK (stage IN ('normalize','match','report','quote')),
    status       text NOT NULL DEFAULT 'pending' CHECK (status IN (
                    'pending','running','succeeded','failed'
                 )),
    attempts     int  NOT NULL DEFAULT 0 CHECK (attempts >= 0),   -- max 3 (§7.2)
    lease_token  text,                          -- owner of the in-flight lease
    leased_at    timestamptz,                   -- lease expiry computed from this
    started_at   timestamptz,
    finished_at  timestamptz,
    error        text,                          -- last failure detail (no payloads)
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_audit_jobs_one_per_stage UNIQUE (run_id, stage)
);

-- Claim + queue-order scan index (partial keeps it small).
CREATE INDEX idx_audit_jobs_claim ON audit_jobs (status, created_at)
    WHERE status IN ('pending','running');

-- ---------------------------------------------------------------------------
-- audit_steps — §7.4 traceability: one row per stage EXECUTION, with pinned
-- model versions, prompt template ids, artifact refs, token counts, duration.
-- ---------------------------------------------------------------------------
CREATE TABLE audit_steps (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            uuid NOT NULL,
    run_id               uuid NOT NULL REFERENCES audit_runs (id) ON DELETE CASCADE,
    stage                text NOT NULL CHECK (stage IN ('normalize','match','report','quote')),
    agent                text NOT NULL,          -- ingest_normalizer | rule_engine |
                                                 -- report_synthesizer | quote_agent (§7.1)
    model_id             text,                   -- pinned; never silently upgraded (§7.4)
    model_version        text,
    prompt_template_id   text,                   -- pinned template id, versioned
    input_artifact_ref   text,                   -- storage_key or hash ref
    output_artifact_ref  text,
    tokens_in            bigint NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out           bigint NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    duration_ms          int,
    status               text NOT NULL CHECK (status IN ('running','succeeded','failed')),
    error                text,
    started_at           timestamptz,
    finished_at          timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_steps_run ON audit_steps (run_id, stage, created_at);

-- ---------------------------------------------------------------------------
-- findings — §8 report schema: one row per rule result, citable by
-- rule_id + standard + version, evidence as JSONB.
-- ---------------------------------------------------------------------------
CREATE TABLE findings (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL,
    run_id           uuid NOT NULL REFERENCES audit_runs (id) ON DELETE CASCADE,
    rule_id          text NOT NULL,
    standard         text NOT NULL,
    standard_version int  NOT NULL DEFAULT 1,
    severity         text NOT NULL CHECK (severity IN ('high','medium','low','info')),
    status           text NOT NULL CHECK (status IN ('passed','failed','needs_review','info')),
    evidence         jsonb NOT NULL DEFAULT '{}'::jsonb,   -- row_refs | cell_refs | excerpts
    llm_judgment     jsonb,                                -- present only for judgment rules
    recommendation   text,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_findings_run_rule ON findings (run_id, rule_id);

-- ---------------------------------------------------------------------------
-- quotes — §9. Drafted by the Quote Agent, EVERY quote must pass through the
-- owner review queue: draft → pending_owner → approved | rejected. No dollar
-- threshold exception (§13 Q5). Client only ever sees approved quotes.
-- ---------------------------------------------------------------------------
CREATE TABLE quotes (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL,
    audit_run_id      uuid NOT NULL REFERENCES audit_runs (id),
    status            text NOT NULL DEFAULT 'draft' CHECK (status IN (
                        'draft','pending_owner','approved','rejected'
                      )),
    amount_usd        numeric(12,2),             -- owner-editable before approval
    currency          text NOT NULL DEFAULT 'USD',
    body              jsonb NOT NULL DEFAULT '{}'::jsonb,  -- itemization from quote_rules.yaml
    requested_at      timestamptz,
    drafted_at        timestamptz,
    decided_at        timestamptz,               -- approved or rejected
    reject_reason     text,                      -- required on reject
    client_visible_at timestamptz,               -- set only on approve (§9.1)
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_quotes_reject_reason CHECK (
        (status = 'rejected' AND reject_reason IS NOT NULL) OR status <> 'rejected'
    )
);

-- Owner queue scan (§9.2): pending quotes first, oldest first.
CREATE INDEX idx_quotes_owner_queue ON quotes (status, created_at)
    WHERE status = 'pending_owner';

-- ---------------------------------------------------------------------------
-- owner_actions — §9.2 audit log of every owner action: who, when, action,
-- before, after. Append-only at MVP (no UPDATE/DELETE grants in prod).
-- ---------------------------------------------------------------------------
CREATE TABLE owner_actions (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL,
    actor       text NOT NULL,                   -- owner identity
    action      text NOT NULL,                   -- quote_approve | quote_reject |
                                                 -- quote_edit | upload_delete | ...
    target_type text NOT NULL,                   -- 'quote' | 'upload' | 'audit_run'
    target_id   uuid NOT NULL,
    before      jsonb,                           -- snapshot before the action
    after       jsonb,                           -- snapshot after the action
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_owner_actions_target ON owner_actions (target_type, target_id, created_at);
