-- Veritas AI Audit Engine — Phase 0.2: reject reason for quarantined uploads.
-- Additive: adds a nullable reject_reason to uploads so the drop zone can tell
-- the client WHY a file was rejected (spoofed type, malformed content, parse
-- over-run — architecture §6.2). Mirrors the quotes.reject_reason pattern (§9).
--
-- NOTE: no BEGIN/COMMIT here — scripts/migrate.py owns the transaction.

ALTER TABLE uploads ADD COLUMN reject_reason text;

ALTER TABLE uploads ADD CONSTRAINT chk_uploads_reject_reason CHECK (
    (status = 'rejected_upload' AND reject_reason IS NOT NULL)
    OR status <> 'rejected_upload'
);
