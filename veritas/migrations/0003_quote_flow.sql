-- Veritas AI Audit Engine — Phase 0.5: quote flow + owner review queue (§9).
-- Additive: enforces ONE quote per audit run and indexes the client-visibility
-- path. The quotes.state machine (draft → pending_owner → approved | rejected,
-- edit → pending_owner) and owner_actions audit log already exist in 0001.
--
-- NOTE: no BEGIN/COMMIT here — scripts/migrate.py owns the transaction.

-- One client-visible quote per audit run (§9.1). The Quote Agent upserts this
-- row on each quote-request; the unique key keeps the client flow idempotent and
-- guarantees there is never more than one quote to approve per run.
CREATE UNIQUE INDEX uq_quotes_one_per_run ON quotes (audit_run_id);

-- Client poll path: approved (client-visible) quotes fastest-first.
CREATE INDEX idx_quotes_client_visible ON quotes (status, client_visible_at)
    WHERE status = 'approved';
