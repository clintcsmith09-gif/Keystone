# Veritas Operations (Phase 0.6 — architecture §13 Q2)

Lean, config-driven operations for the Veritas MVP host. No paid services:
automated full backups via cron/systemd timer, 24/7 uptime via a portable
watchdog (or systemd on hosts that have it), and an in-app owner review queue
for all operator-visible alerts (no third-party email at MVP).

## What is backed up

`backup.sh` produces one timestamped directory per run:

```
<VERITAS_BACKUP_DIR>/20260906-021700/
  db.dump            # logical Postgres dump (pg_dump -Fc, no master key inside)
  storage.tar.gz     # encrypted object store blobs (already at rest-encrypted)
```

Only the newest `VERITAS_KEEP_BACKUPS` (default 7) backups are retained; older
directories are deleted by the script itself.

## Backing up

```bash
# defaults: VERITAS_DATABASE_URL=postgresql://localhost/veritas,
# storage ./data/objects, backups ./data/backups, keep 7
VERITAS_DATABASE_URL=postgresql://engine:veritas_dev@localhost/veritas \
  VERITAS_STORAGE_ROOT=/home/agent-lead/Keystone/veritas/data/objects \
  VERITAS_BACKUP_DIR=/home/agent-lead/Keystone/veritas/data/backups \
  scripts/backup.sh
```

The script prints the backup directory path on success — schedulers/tests read
stdout. It is idempotent and safe to run any number of times.

### Nightly automation (systemd)

```bash
sudo cp ops/veritas-backup.service ops/veritas-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now veritas-backup.timer
systemctl list-timers veritas-backup.timer   # next run at 02:17
```

The `.service` unit reads `veritas/.env` for `VERITAS_DATABASE_URL`,
`VERITAS_STORAGE_ROOT`, `VERITAS_BACKUP_DIR` (create `veritas/.env` from
`.env.example` first). `OnBootSec` is covered by `Persistent=true` so a rebooted
host still gets its nightly backup.

## Restoring

```bash
scripts/restore.sh <backup_dir> [target_db_url]
# e.g.
scripts/restore.sh /home/agent-lead/Keystone/veritas/data/backups/20260906-021700
```

* Database: `pg_restore --clean --if-exists --no-owner` — the backup snapshot is
  authoritative; existing objects are replaced.
* Object store: `storage.tar.gz` is unpacked in place (destructive replace).
* **Master key:** restore does not and cannot restore the master key (it is
  never stored in the DB). Keep the same `VERITAS_MASTER_KEY` the backup was
  taken under, or the encrypted blobs will not decrypt.

### Round-trip test (proves a backup can be restored)

```bash
export VERITAS_DATABASE_URL=postgresql://engine:veritas_dev@localhost/veritas
export VERITAS_STORAGE_ROOT=/tmp/veritas-rt-storage
export VERITAS_BACKUP_DIR=/tmp/veritas-rt-backups
# 1. put data in
mkdir -p "$VERITAS_STORAGE_ROOT" && echo hello > "$VERITAS_STORAGE_ROOT/blob.bin"
psql "$VERITAS_DATABASE_URL" -c "CREATE TABLE IF NOT EXISTS rt_probe (v text); INSERT INTO rt_probe VALUES ('rt-ok');"
# 2. back up
BK=$(scripts/backup.sh)
# 3. destroy the live copies
rm -rf "$VERITAS_STORAGE_ROOT"; psql "$VERITAS_DATABASE_URL" -c "DROP TABLE rt_probe;"
# 4. restore
scripts/restore.sh "$BK"
test -f "$VERITAS_STORAGE_ROOT/blob.bin" && psql "$VERITAS_DATABASE_URL" -tAc \
  "SELECT count(*) FROM rt_probe WHERE v='rt-ok';"   # -> 1
```

## 24/7 uptime

Two interchangeable mechanisms (both reduce to: probe /health, restart on
failure). The portable watchdog is what this MVP box runs; systemd is preferred
when the host has it.

### Portable watchdog (`scripts/watchdog.sh`)

```bash
VERITAS_ALERT_LOG=/var/log/veritas-watchdog.log \
  VERITAS_SERVICE_CMD="/opt/veritas-venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 3000" \
  setsid nohup scripts/watchdog.sh >/dev/null 2>&1 &
```

* Probes `VERITAS_HEALTH_URL` (`http://127.0.0.1:3000/health`) every
  `VERITAS_WATCH_INTERVAL` seconds (default 10).
* Starts the service if the probe is down at boot (within a grace window).
* After `VERITAS_MAX_FAILS` (default 3) consecutive failed probes it restarts
  the service (pkill uvicorn, then start) and resets the counter.
* Every transition is appended to `VERITAS_ALERT_LOG` — a durable, greppable
  alert record (no third-party email at MVP; the in-app owner review queue
  surfaces the same issues).

### systemd unit (`ops/veritas.service`)

```bash
sudo cp ops/veritas.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now veritas
```

* `Restart=always` with `RestartSec=5` provides restart-on-failure.
* The unit reads `veritas/.env` for the database URL, storage root and master
  key; it expects the venv at `/opt/veritas-venv` (adjust `ExecStart` if not).

## Owner-alert surface (in-app)

Cost-gate halts, monthly 80% warnings and monthly-cap queueing all create rows
in `owner_notifications`, exposed under `/api/v1/owner/notifications` and
acknowledged via `/api/v1/owner/notifications/{id}/ack`. Cap-queued audits sit
in the same review surface as quotes
(`/api/v1/owner/cost-gate` summary + `/api/v1/owner/approvals/{run_id}/approve`).
The monthly aggregate LLM spend view lives at `/api/v1/telemetry/monthly`.

## Cost gate knobs (`VERITAS_`, see .env.example)

| Setting | Default | Meaning |
| --- | --- | --- |
| `COST_GATE_ENABLED` | true | master switch for the token-budget gate |
| `COST_GATE_MAX_TOKENS_IN` | 250000 | per-audit hard in-token budget |
| `COST_GATE_MAX_TOKENS_OUT` | 75000 | per-audit hard out-token budget |
| `COST_GATE_MONTHLY_TOKENS_IN` | 2000000 | monthly aggregate in-token cap |
| `COST_GATE_MONTHLY_TOKENS_OUT` | 600000 | monthly aggregate out-token cap |
| `COST_GATE_MONTHLY_WARN_RATIO` | 0.80 | fraction of cap that warns the owner |
| `MONTHLY_REVENUE_USD` + price | 0 | when both set, caps derive from 20% of revenue |

At MVP no provider is wired, so the gate never spends money: it is a pure,
deterministic token-budget computation. When a provider + per-1M-token price are
configured (Phase 1), the same machinery reports USD and can derive the monthly
cap from the §11.3 revenue ratio.