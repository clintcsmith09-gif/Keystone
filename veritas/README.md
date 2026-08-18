# Veritas service — Phase 0 foundation

The **Veritas AI Audit Engine** MVP service. One FastAPI app + Postgres job queue +
local encrypted object storage, per the ratified architecture
(`/home/team/shared/sprint2-architecture.md`, RATIFIED 2026-08-17).

**Scope of this scaffold (Phase 0.1):** app entry, env-based config, `/health`,
Postgres schema + migrations (7 business tables, `tenant_id` on every table, state
machines matching §4.2/§7.2/§9), and the `StorageBackend` interface with a local
encrypted-disk implementation (envelope encryption, AES-256-GCM, master key from
env). **No LLM calls, no third-party API spend, no heavy deps.**

## Layout (why a `veritas/` subdir)

The repo root README is the repository-level README (kept untouched). The product
service lives in its own directory so that the later Phase 0 additions — the Vite
client, the YAML rule sets (`veritas/rules/`), deployment config — can sit beside it
without mixing concerns. Rule sets for ISO 27001 and PCI-DSS scoped subsets (§13 Q1)
are authored in a later sprint; `rules/` is stubbed below.

```
veritas/
├── app/
│   ├── main.py          # FastAPI entry, /health
│   ├── config.py        # env-based Settings (VERITAS_* vars, .env)
│   └── storage/         # StorageBackend interface + local encrypted backend
├── migrations/          # SQL migrations (applied in filename order)
├── scripts/migrate.py   # idempotent migration runner
├── rules/               # YAML rule sets (ISO-27001, PCI-DSS) — later sprint
└── tests/
```

## Prerequisites

- Python 3.11+ (developed on 3.12)
- PostgreSQL 13+ (16 on the dev box). `gen_random_uuid()` is core in PG 13+.

## Dev setup

```bash
cd veritas

# 1. Environment (pick one — uv or pip)
uv venv && uv pip install -e '.[dev]'        # uv
# or: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'

# 2. Config — never commit a real .env
cp .env.example .env                          # edit VERITAS_DATABASE_URL etc.

# 3. Database + migrations
createdb veritas                              # or: sudo -u postgres createdb veritas
python scripts/migrate.py                     # idempotent; safe to re-run

# 4. Run the service
uv run uvicorn app.main:app --reload          # http://127.0.0.1:8000/health

# 5. Tests (unit; DB tests need a scratch DB)
pytest                                       # storage + health tests
VERITAS_TEST_DATABASE_URL=postgresql://localhost/veritas_test pytest   # + migrations
```

Check the app:

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok","service":"veritas","version":"0.1.0","environment":"dev","database":"ok"}
```

## Configuration (env vars, prefix `VERITAS_`)

| Variable | Default | Meaning |
|---|---|---|
| `VERITAS_DATABASE_URL` | `postgresql://localhost/veritas` | psycopg v3 DSN |
| `VERITAS_STORAGE_ROOT` | `./data/objects` | encrypted object store root |
| `VERITAS_MASTER_KEY` | *(none)* | base64/hex 32-byte key; storage refuses to start without it |
| `VERITAS_ENVIRONMENT` | `dev` | dev/staging/prod |
| `VERITAS_UPLOAD_MAX_BYTES` | `104857600` | 100 MB upload cap (§6.2/§13 Q4) |

Generate a real master key: `python -c "import secrets,base64;print(base64.b64encode(secrets.token_bytes(32)).decode())"`

## Schema notes

- **State machines are enforced in SQL CHECK constraints** (single source of truth):
  - `audit_runs.status` — §4.2: `uploaded → validating → storing → queued →
    normalizing → matching → reporting → completed | failed | cost_gate_halted |
    rejected_upload`
  - `audit_jobs` — §7.2: stage `normalize|match|report|quote`, status
    `pending|running|succeeded|failed`, `attempts` (max 3), `lease_token`/`leased_at`
    for `FOR UPDATE SKIP LOCKED` claiming. No Redis.
  - `quotes.status` — §9: `draft → pending_owner → approved | rejected`, with a
    CHECK that a rejection carries a reason. Every quote goes through the owner
    queue; **no dollar-threshold exception** (§13 Q5).
- `tenant_id uuid NOT NULL` on every business table (§10.3) — one real tenant at MVP.
- `owner_actions` is the append-only log of every owner decision
  (`who, when, action, before, after` — §9.2).
- The 100 MB upload cap and 50–100 MB audit sizing follow §13 Q4.

## Storage backend

`app/storage/` implements architecture §6.4: envelope encryption with a fresh
per-object 256-bit data key, wrapped by the master key from the environment;
AES-256-GCM everywhere, so every read verifies integrity (tamper → `StorageIntegrityError`).
`delete()` overwrites with random bytes before unlinking (§10.4 retention intent).
Swap to an S3-compatible managed tier later by adding a backend behind the same
`StorageBackend` interface — no call-site changes.
