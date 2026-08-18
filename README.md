# Keystone
A lean, multi-tenant B2B SaaS platform sold on subscription — built to stay cheap to run while we find product-market fit. (Working assumption: B2B SaaS, pre-revenue, MVP stage.)

## Repo layout

- `veritas/` — Veritas AI Audit Engine MVP service (FastAPI + Postgres + encrypted storage; Phase 0 foundation). Dev-run instructions and architecture references in `veritas/README.md`. Kept in its own directory so the Vite client, rule sets, and deployment config can sit beside it later.
- `veritas/rules/` — YAML compliance rule sets (ISO 27001, PCI-DSS scoped subsets; §13 Q1 of the ratified architecture).
