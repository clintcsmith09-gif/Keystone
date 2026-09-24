# Veritas audit quality — Block A (labeled corpus + accuracy harness)

Findings are only worth shipping if they are *measurably* correct. This directory
turns "the engine runs" into "we can prove how accurate the engine is": a
ground-truth labeled corpus, a provider-agnostic precision/recall harness, and
per-rule accuracy artifacts.

Nothing here changes engine behaviour — the harness calls the same
`app.audit.normalize` / `app.audit.matcher` / `app.audit.rules` code the
pipeline calls. Everything is additive test/measurement code.

## Run it

```bash
cd veritas

python -m quality.corpus check     # committed corpus == generator output? labels resolve?
python -m quality.harness          # noop (offline, deterministic) -> writes a report artifact
python -m quality.harness --no-write
python -m pytest tests/test_quality_accuracy.py -q   # the regression guard

# Real model (manual/optional; no live calls in the test suite):
VERITAS_LLM_PROVIDER=anthropic python -m quality.harness
```

`VERITAS_LLM_PROVIDER=anthropic` needs `ANTHROPIC_API_KEY` in the environment;
without a key the harness prints why and exits 2 — it never silently falls back
to noop and never prints the key. Enable zero-retention on the Anthropic account
before any real-customer run (see `app/audit/llm.py`).

## What is measured

* **Comparison key:** `(dataset file, rule_id)` — a rule "fires" when its result
  is `status='failed'`. Severity is a property of the rule, carried alongside the
  row. TP = fired where labeled; FP = fired where nothing was labeled wrong
  (the number that matters most for a compliance product); FN = silent where an
  anomaly was seeded; TN = correctly silent.
* **Secondary, row-level key:** `(dataset file, rule_id, row index)` for the
  row-scoped check types (`format`, per-row `threshold`), using the offending
  row indices the matcher publishes in its evidence. A rule that fires on the
  *wrong* rows produces bad evidence, so it is measured too.
* **Out of scope:** `check_type: judgment` / `llm_assist: true` rules. They always
  emit `needs_review` and there is no ground truth for "a human should look at
  this", so they are listed in the report and never counted as TP/FP/FN.

`quality/reports/accuracy-<provider>-<UTC ts>.json|.md` carries the per-rule
TP/FP/FN/TN + precision/recall/F1 table, overall totals, per-dataset detail,
which rules were never exercised, the judgment-rule list, and the corpus's
coverage gaps.

## The corpus

Seven CSV datasets in `quality/corpus/` (single-table "ledger" shape, the same
view the Normalize stage emits), 55 rows, 31 labeled rule-fires and 17 labeled
row-fires:

| dataset | rows | what it proves |
|---|---|---|
| `clean_ledger.csv` | 12 | fully compliant → every deterministic rule must stay silent |
| `identity_anomalies.csv` | 14 | duplicate/empty/whitespace identifiers, bad role, status, timestamp, source IP, email |
| `missing_columns_ledger.csv` | 6 | required columns dropped → presence + missing-column branches |
| `sensitive_columns_ledger.csv` | 5 | `cvv`/`ssn` present → exactly one rule may fire |
| `empty_ledger.csv` | 0 | header-only → aggregate row-count rules |
| `card_anomalies.csv` | 10 | PAN format/emptiness/duplication, expiry, negative amount, currency, empty key |
| `unmapped_anomalies.csv` | 8 | real anomaly classes with **no** scoped rule → must stay silent; recall gap is reported |

Labels live in `manifest.json`: `expected_failures` (dataset-scoped rules) plus
`row_expectations` (the exact 0-based row a row-scoped rule must flag), each
carrying `sha256` for staleness detection. Every label is validated against the
loaded rule sets, so a typo fails loudly instead of inflating a score.

## Extending it

1. Add the seeded row/column override in `quality/corpus.py`, with the rule id it
   must trigger (dataset-level → `expected_failures`; row-level →
   `row_expectations`).
2. `python -m quality.corpus write && python -m quality.corpus check`
3. `python -m quality.harness` — re-read the report. A new FALSE POSITIVE or FALSE
   NEGATIVE is a finding about the engine, not about the harness: investigate
   before touching expectations. The regression test will fail until it is resolved.

## Baseline

Noop provider, 2026-09-24: 301 dataset×rule cells over 43 deterministic rules —
tp 31, fp 0, fn 0, tn 270 → precision 1.000, recall 1.000, F1 1.000; row-level
17/0/0; 8 judgment rules unscored; 13 rules exercised as true negatives only.
Floors enforced by the suite: precision and recall ≥ 0.95 per rule and overall.

Coverage gaps found and reported (no scoped rule can detect these — new rule
types needed, deliberately out of scope for Block A's additive constraint):
off-hours privileged logins, failed-login spikes, MFA gaps on privileged
accounts, high-value outliers, out-of-hours foreign access.
