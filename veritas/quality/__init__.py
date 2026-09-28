"""Block A — audit-quality & trust foundation (architecture §7.3).

Two things live here:

* ``corpus``  — the deterministic, ground-truth **labeled** audit corpus
  (CSV datasets + ``manifest.json`` mapping every seeded anomaly to the ISO
  27001 / PCI-DSS rule id it must trigger), regenerable from a fixed seed.
* ``harness`` — the provider-agnostic precision/recall runner that drives the
  engine's real Normalize -> Match stages over that corpus and reports per-rule
  accuracy (JSON + Markdown artifacts).

Nothing here changes engine behaviour: the harness imports the same
``app.audit.normalize`` / ``app.audit.matcher`` / ``app.audit.rules`` code the
pipeline runs, and the corpus is additive test data.
"""
