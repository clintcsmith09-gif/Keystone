"""Block A — provider-agnostic precision/recall harness for the Veritas engine.

WHAT IT DOES
------------
Runs the engine's REAL Normalize -> Match stages (``app.audit.normalize`` +
``app.audit.matcher`` + ``app.audit.rules`` — the same code the pipeline runs,
no re-implementation) over the labeled corpus in ``quality/corpus/`` and scores
the emitted findings against the ground-truth labels in ``manifest.json``.

COMPARISON KEY (what "correct" means here)
------------------------------------------
Primary: ``(dataset file, rule_id)`` — the dataset artifact the engine was
pointed at, plus the rule that fired. A rule fires when its result is
``status='failed'``. For each cell:

* TP — the rule fired on the dataset whose labels say it must fire;
* FP — the rule fired on a dataset whose labels say it must not (this is the
  number that matters most for a compliance product: a confident wrong finding);
* FN — the rule stayed silent on a dataset with a seeded, labeled anomaly
  (the engine missed real evidence);
* TN — silence where silence was expected.

Severity is a property of the rule (the matcher distinguishes outcomes, not
severity levels), so it is carried alongside each row rather than being part of
the key. Row-level scoring (secondary) uses ``(dataset file, rule_id, row
index)`` for the row-scoped check types (``format`` / per-row ``threshold``),
where the engine publishes the offending row indices in its evidence — a rule
that fires on the *wrong* rows is not correct evidence, so it is measured.

SCOPE OF SCORING
----------------
Only deterministic rules (``check_type`` in presence/threshold/format/reference
with ``llm_assist: false``) are scored: their outcome is reproducible and the
corpus can state ground truth for them. ``judgment`` / ``llm_assist`` rules
always emit ``needs_review`` — there is no ground truth for "a human should
look at this" — so they are listed separately and never counted as TP/FP/FN.

PROVIDER
--------
The harness takes any ``LLMClient`` from the seam. Default is the offline,
deterministic ``noop`` provider (all tests use it; no network). The real
``anthropic`` provider runs the SAME corpus and the SAME scoring code::

    cd veritas && VERITAS_LLM_PROVIDER=anthropic python -m quality.harness

It skips cleanly (exit 2, no calls) when ``ANTHROPIC_API_KEY`` is absent, and
with the noop provider the judgment rules never influence the matrix — so
swapping providers only ever changes the ``needs_review`` verdicts, and the
deterministic matrix should be byte-identical (that is asserted in the suite
against a mocked Anthropic transport).

ARTIFACTS
---------
``quality/reports/accuracy-<provider>-<UTC ts>.json`` and ``.md`` — per-rule TP/
FP/FN/TN + precision/recall/F1, overall totals, per-dataset detail, judgment
coverage, and the corpus's unmapped-anomaly coverage gaps.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.audit import matcher, normalize
from app.audit.llm import AnthropicLLMClient, LLMClient, NoopLLMClient
from app.audit.rules import RULES_DIR, load_all

from . import corpus as corpus_mod

BLOCK = "A"
REPORT_NAME = "audit-accuracy"
REPORTS_DIR = Path(__file__).resolve().parent / "reports"

# Regression floors for the deterministic matrix. Set from the measured noop
# baseline (see the committed accuracy-noop-* artifacts): the engine is exactly
# right on this corpus today, so 1.0 would "pass" but would leave no headroom
# for an honest rule refinement. 0.95 is the owner-facing quality bar: at most
# one in twenty rule outcomes may be wrong, per rule as well as overall.
FLOORS = {"precision": 0.95, "recall": 0.95}

ROW_SCOPED_CHECK_TYPES = ("format", "threshold")


class ProviderUnavailable(RuntimeError):
    """A provider was requested that cannot be used in this environment (e.g.
    anthropic without an API key). Callers may treat this as a clean skip."""


def build_llm(provider: str | None = None) -> LLMClient:
    """Construct the provider client from the seam.

    ``provider`` defaults to ``$VERITAS_LLM_PROVIDER`` (then ``noop``), so the
    harness picks up the same switch the pipeline uses. The API key is read from
    the environment and never logged.
    """
    name = (provider or os.environ.get("VERITAS_LLM_PROVIDER") or "noop").strip().lower()
    if name == "noop":
        return NoopLLMClient(model_id="noop-llm", model_version="0.1.0", response="{}")
    if name == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("VERITAS_ANTHROPIC_API_KEY") or ""
        if not key.strip():
            raise ProviderUnavailable(
                "provider 'anthropic' needs ANTHROPIC_API_KEY in the environment; "
                "the noop provider remains the default and is unaffected."
            )
        return AnthropicLLMClient(
            api_key=key,
            model=os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-5",
        )
    raise ProviderUnavailable(
        f"unknown provider {name!r}: choose 'noop' (offline) or 'anthropic' (real Claude API)."
    )


# --- corpus + engine driving ---------------------------------------------------
def load_corpus(corpus_dir: Path | None = None) -> tuple[dict, dict[str, bytes]]:
    corpus_dir = Path(corpus_dir or corpus_mod.CORPUS_DIR)
    manifest = corpus_mod.load_manifest(corpus_dir)
    files = {f["name"]: (corpus_dir / f["name"]).read_bytes() for f in manifest["files"]}
    return manifest, files


def _normalize_file(name: str, payload: bytes) -> dict:
    return normalize.normalize(payload, kind=normalize.kind_from_path(name), source=name)


async def _run_all(manifest: dict, files: dict[str, bytes], llm: LLMClient,
                   rule_sets: dict) -> list[dict]:
    runs: list[dict] = []
    for entry in manifest["files"]:
        name = entry["name"]
        view = _normalize_file(name, files[name])
        for standard in sorted(rule_sets):
            results = await matcher.match_view(rule_sets[standard], view, llm)
            runs.append({
                "file": name,
                "standard": standard,
                "row_count": view.get("row_count", 0),
                "results": results,
            })
    return runs


def run_engine(manifest: dict, files: dict[str, bytes], llm: LLMClient,
               rules_dir: Path | None = None) -> list[dict]:
    """Drive Normalize -> Match over every corpus dataset for every rule set."""
    rule_sets = load_all(Path(rules_dir) if rules_dir else None)
    return asyncio.run(_run_all(manifest, files, llm, rule_sets))


# --- scoring -------------------------------------------------------------------
def _ratio(num: int, den: int) -> float | None:
    return (num / den) if den else None


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _blank_counters(rule) -> dict:
    return {
        "rule_id": rule.id,
        "standard": None,          # filled by the caller
        "category": rule.category,
        "severity": rule.severity,
        "check_type": rule.check_type,
        "tp": 0, "fp": 0, "fn": 0, "tn": 0,
    }


def score(manifest: dict, runs: list[dict], rules_dir: Path | None = None) -> dict:
    """Compare emitted findings against the corpus labels."""
    rule_sets = load_all(Path(rules_dir) if rules_dir else None)
    labels = {f["name"]: f for f in manifest["files"]}

    per_rule: dict[str, dict] = {}
    per_file: dict[str, dict] = {}
    per_file_std: dict[tuple[str, str], dict] = {}
    row_level: dict[str, dict] = {}
    judgments: list[dict] = []

    for run in runs:
        rs = rule_sets[run["standard"]]
        fname = run["file"]
        label = labels[fname]
        expected = set(label["expected_failures"])
        row_expect: dict[str, set[int]] = {}
        for rex in label["row_expectations"]:
            for rid in rex["expects"]:
                row_expect.setdefault(rid, set()).add(rex["index"])

        fsum = per_file.setdefault(fname, {
            "file": fname, "rows": run["row_count"], "expected_failures": sorted(expected),
            "tp": 0, "fp": 0, "fn": 0, "tn": 0, "fired": [], "missed": [], "unexpected": [],
        })
        st_sum = per_file_std.setdefault((fname, run["standard"]), {
            "file": fname, "standard": run["standard"], "rules": len(rs.rules),
            "tp": 0, "fp": 0, "fn": 0, "tn": 0, "judgment_rules": 0,
        })

        for res in run["results"]:
            rule = rs.by_id(res["rule_id"])
            if rule is None:  # pragma: no cover - rule sets are consistent
                continue
            if rule.check_type == "judgment" or rule.llm_assist:
                st_sum["judgment_rules"] += 1
                judgments.append({
                    "file": fname, "standard": run["standard"], "rule_id": rule.id,
                    "status": res["status"],
                })
                continue

            counters = per_rule.setdefault(rule.id, _blank_counters(rule))
            counters["standard"] = run["standard"]
            fired = res["status"] == "failed"
            should = rule.id in expected
            if fired and should:
                counters["tp"] += 1
                fsum["tp"] += 1
                st_sum["tp"] += 1
            elif fired and not should:
                counters["fp"] += 1
                fsum["fp"] += 1
                st_sum["fp"] += 1
                fsum["unexpected"].append(rule.id)
            elif not fired and should:
                counters["fn"] += 1
                fsum["fn"] += 1
                st_sum["fn"] += 1
                fsum["missed"].append(rule.id)
            else:
                counters["tn"] += 1
                fsum["tn"] += 1
                st_sum["tn"] += 1
            if fired:
                fsum["fired"].append(rule.id)

            # --- row-level (secondary): only where labels pin rows and the
            # engine publishes the offending row indices.
            if rule.id in row_expect and rule.check_type in ROW_SCOPED_CHECK_TYPES:
                errs = (res.get("evidence") or {}).get("violations") or []
                emitted = {int(i) for i in errs if isinstance(i, (int, float, str)) and str(i).isdigit()}
                want = row_expect[rule.id]
                rl = row_level.setdefault(rule.id, {
                    "rule_id": rule.id, "tp": 0, "fp": 0, "fn": 0, "datasets": 0, "truncated": False,
                })
                rl["datasets"] += 1
                rl["tp"] += len(want & emitted)
                rl["fp"] += len(emitted - want)
                rl["fn"] += len(want - emitted)
                if len(errs) >= matcher._MAX_VIOLATIONS:
                    rl["truncated"] = True

    for c in per_rule.values():
        c["support"] = c["tp"] + c["fn"]
        c["predicted"] = c["tp"] + c["fp"]
        c["precision"] = _ratio(c["tp"], c["tp"] + c["fp"])
        c["recall"] = _ratio(c["tp"], c["support"])
        c["f1"] = _f1(c["precision"], c["recall"])

    overall = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for c in per_rule.values():
        for k in overall:
            overall[k] += c[k]
    scored_rules = sorted(per_rule.values(), key=lambda c: c["rule_id"])

    below = [
        {"rule_id": c["rule_id"], "precision": c["precision"], "recall": c["recall"]}
        for c in scored_rules
        if (c["precision"] is not None and c["precision"] < FLOORS["precision"])
        or (c["recall"] is not None and c["recall"] < FLOORS["recall"])
    ]
    never_exercised = [
        c["rule_id"] for c in scored_rules if c["support"] == 0 and c["predicted"] == 0
    ]

    row_totals = {"tp": 0, "fp": 0, "fn": 0}
    for rl in row_level.values():
        for k in row_totals:
            row_totals[k] += rl[k]

    return {
        "per_rule": scored_rules,
        "per_file": sorted(per_file.values(), key=lambda f: f["file"]),
        "per_file_standard": sorted(per_file_std.values(), key=lambda f: (f["file"], f["standard"])),
        "overall": {
            **overall,
            "cells_scored": sum(overall.values()),
            "precision": _ratio(overall["tp"], overall["tp"] + overall["fp"]),
            "recall": _ratio(overall["tp"], overall["tp"] + overall["fn"]),
            "f1": _f1(_ratio(overall["tp"], overall["tp"] + overall["fp"]),
                      _ratio(overall["tp"], overall["tp"] + overall["fn"])),
            "rules_scored": len(scored_rules),
            "rules_never_exercised": never_exercised,
            "judgment_rules_unscored": len({j["rule_id"] for j in judgments}),
        },
        "row_level": {
            "totals": {
                **row_totals,
                "precision": _ratio(row_totals["tp"], row_totals["tp"] + row_totals["fp"]),
                "recall": _ratio(row_totals["tp"], row_totals["tp"] + row_totals["fn"]),
            },
            "per_rule": sorted(row_level.values(), key=lambda r: r["rule_id"]),
        },
        "judgments": sorted(judgments, key=lambda j: (j["file"], j["rule_id"])),
        "below_floor": below,
    }


# --- report -------------------------------------------------------------------
def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_report(manifest: dict, files: dict[str, bytes], scoring: dict, *,
                 provider: str, llm: LLMClient, corpus_dir: Path | None = None,
                 rules_dir: Path | None = None, generated_at: str | None = None) -> dict:
    return {
        "report": REPORT_NAME,
        "block": BLOCK,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "model_id": getattr(llm, "model_id", None),
        "model_version": getattr(llm, "model_version", None),
        "comparison_key": (
            "dataset artifact + rule_id (rule fires when status='failed'); "
            "row-level secondary key adds the 0-based data row index for "
            "format/per-row-threshold rules"
        ),
        "scoring_scope": (
            "deterministic rules only (presence/threshold/format/reference, "
            "llm_assist=false); judgment/llm_assist rules emit needs_review and "
            "are reported separately"
        ),
        "floors": dict(FLOORS),
        "corpus": {
            "version": manifest["corpus_version"],
            "seed": manifest["seed"],
            "files": manifest["totals"]["files"],
            "rows": manifest["totals"]["rows"],
            "expected_rule_fires": manifest["totals"]["expected_rule_fires"],
            "expected_row_level_fires": manifest["totals"]["expected_row_level_fires"],
            "uncovered_anomaly_rows": manifest["totals"]["uncovered_anomaly_rows"],
            "integrity_problems": corpus_mod.verify_corpus(corpus_dir),
            "dir": str(Path(corpus_dir or corpus_mod.CORPUS_DIR)),
        },
        "rules_dir": str(Path(rules_dir or RULES_DIR)),
        "overall": scoring["overall"],
        "per_rule": scoring["per_rule"],
        "per_file": scoring["per_file"],
        "per_file_standard": scoring["per_file_standard"],
        "row_level": scoring["row_level"],
        "judgments": scoring["judgments"],
        "below_floor": scoring["below_floor"],
        "coverage_gaps": manifest.get("coverage_gaps", []),
    }


def _fmt(value) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def render_markdown(report: dict) -> str:
    o = report["overall"]
    c = report["corpus"]
    lines: list[str] = []
    lines.append("# Veritas audit-accuracy report (Block A)")
    lines.append("")
    lines.append(f"- Generated: {report['generated_at']}")
    lines.append(f"- Provider: `{report['provider']}` (model `{report['model_id']}`, "
                 f"pinned version `{report['model_version']}`)")
    lines.append(f"- Corpus: v{c['version']} seed {c['seed']} — {c['files']} datasets, "
                 f"{c['rows']} rows, {c['expected_rule_fires']} labeled rule-fires, "
                 f"{c['expected_row_level_fires']} labeled row-fires")
    lines.append(f"- Corpus integrity: "
                 f"{'OK' if not c['integrity_problems'] else 'PROBLEMS: ' + '; '.join(c['integrity_problems'])}")
    lines.append(f"- Comparison key: {report['comparison_key']}")
    lines.append(f"- Scoring scope: {report['scoring_scope']}")
    lines.append("")
    lines.append("## Overall (deterministic rules)")
    lines.append("")
    lines.append("| tp | fp | fn | tn | precision | recall | F1 | rules scored |")
    lines.append("|---|---|---|---|---|---|---|---|")
    lines.append(
        f"| {o['tp']} | {o['fp']} | {o['fn']} | {o['tn']} | {_fmt(o['precision'])} | "
        f"{_fmt(o['recall'])} | {_fmt(o['f1'])} | {o['rules_scored']} |"
    )
    lines.append("")
    floors = report["floors"]
    lines.append(f"Regression floors: precision >= {floors['precision']}, "
                 f"recall >= {floors['recall']} (per rule and overall).")
    lines.append("")
    rl = report["row_level"]["totals"]
    lines.append(f"Row-level (secondary, format + per-row threshold): "
                 f"tp {rl['tp']}, fp {rl['fp']}, fn {rl['fn']} — "
                 f"precision {_fmt(rl['precision'])}, recall {_fmt(rl['recall'])}.")
    lines.append("")
    lines.append("## Per rule")
    lines.append("")
    lines.append("| rule_id | standard | severity | check_type | tp | fp | fn | tn | "
                 "precision | recall | F1 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in report["per_rule"]:
        lines.append(
            f"| `{r['rule_id']}` | {r['standard']} | {r['severity']} | {r['check_type']} | "
            f"{r['tp']} | {r['fp']} | {r['fn']} | {r['tn']} | {_fmt(r['precision'])} | "
            f"{_fmt(r['recall'])} | {_fmt(r['f1'])} |"
        )
    lines.append("")
    lines.append("## Per dataset")
    lines.append("")
    lines.append("| dataset | rows | expected fires | tp | fp | fn | missed (FN) | unexpected (FP) |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for f in report["per_file"]:
        lines.append(
            f"| `{f['file']}` | {f['rows']} | {len(f['expected_failures'])} | {f['tp']} | "
            f"{f['fp']} | {f['fn']} | {', '.join(f['missed']) or '-'} | "
            f"{', '.join(f['unexpected']) or '-'} |"
        )
    lines.append("")
    lines.append("## Rules below floor / never exercised")
    lines.append("")
    if report["below_floor"]:
        for b in report["below_floor"]:
            lines.append(f"- `{b['rule_id']}` — precision {_fmt(b['precision'])}, "
                         f"recall {_fmt(b['recall'])}")
    else:
        lines.append("- none: every scored rule meets the floors")
    never = o.get("rules_never_exercised") or []
    lines.append("")
    lines.append(f"Never exercised (no labeled anomaly and never fired — precision-only "
                 f"evidence): {', '.join('`%s`' % r for r in never) if never else 'none'}")
    lines.append("")
    lines.append("## Judgment rules (unscored by design)")
    lines.append("")
    jud = sorted({j["rule_id"] for j in report["judgments"]})
    lines.append(f"{len(jud)} judgment/llm_assist rules emitted `needs_review` over the corpus "
                 f"and are excluded from precision/recall (no ground truth): "
                 f"{', '.join('`%s`' % r for r in jud) if jud else 'none'}")
    lines.append("")
    lines.append("## Covered anomaly classes with NO scoped rule (recall gaps)")
    lines.append("")
    if report["coverage_gaps"]:
        lines.append("| class | dataset | row | why it cannot be detected |")
        lines.append("|---|---|---|---|")
        for g in report["coverage_gaps"]:
            lines.append(f"| {g['class']} | `{g['file']}` | {g['row_index']} | {g['why']} |")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## Running this harness against the real provider")
    lines.append("")
    lines.append("```bash")
    lines.append("cd veritas")
    lines.append("python -m quality.harness                       # noop (offline, deterministic)")
    lines.append("VERITAS_LLM_PROVIDER=anthropic python -m quality.harness   # real Claude API")
    lines.append("```")
    lines.append("")
    lines.append("The anthropic run makes no difference to the deterministic matrix (the "
                 "judgment rules it changes are out of scope); it exists to record real "
                 "model verdicts and token telemetry alongside the same measurements.")
    lines.append("")
    return "\n".join(lines)


def write_report(report: dict, reports_dir: Path | None = None,
                 label: str | None = None) -> tuple[Path, Path]:
    reports_dir = Path(reports_dir or REPORTS_DIR)
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.get("stamp") or _ts()
    stem = f"accuracy-{report['provider']}-{label + '-' if label else ''}{stamp}"
    json_path = reports_dir / f"{stem}.json"
    md_path = reports_dir / f"{stem}.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


# --- entry points ---------------------------------------------------------------
def run_accuracy(*, provider: str | None = None, corpus_dir: Path | None = None,
                 rules_dir: Path | None = None, llm: LLMClient | None = None,
                 generated_at: str | None = None) -> dict:
    """Run + score the corpus and return the report dict (no files written)."""
    manifest, files = load_corpus(corpus_dir)
    llm = llm or build_llm(provider)
    provider_name = provider or os.environ.get("VERITAS_LLM_PROVIDER") or "noop"
    runs = run_engine(manifest, files, llm, rules_dir)
    scoring = score(manifest, runs, rules_dir)
    return build_report(manifest, files, scoring, provider=provider_name, llm=llm,
                        corpus_dir=corpus_dir, rules_dir=rules_dir,
                        generated_at=generated_at)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m quality.harness",
        description="Run the Block A labeled-corpus accuracy harness.",
    )
    parser.add_argument("--provider", default=None,
                        help="noop (default) or anthropic; also honours VERITAS_LLM_PROVIDER")
    parser.add_argument("--corpus-dir", default=None)
    parser.add_argument("--reports-dir", default=None)
    parser.add_argument("--label", default=None, help="extra filename tag for the artifact")
    parser.add_argument("--no-write", action="store_true", help="score only, write no artifact")
    args = parser.parse_args(argv)

    try:
        report = run_accuracy(provider=args.provider, corpus_dir=args.corpus_dir)
    except ProviderUnavailable as exc:
        print(f"cannot run provider: {exc}", file=sys.stderr)
        return 2

    report["stamp"] = _ts()
    if not args.no_write:
        json_path, md_path = write_report(report, args.reports_dir, args.label)
        print(f"wrote {json_path}")
        print(f"wrote {md_path}")

    o = report["overall"]
    print(f"provider={report['provider']} model={report['model_id']}")
    print(f"cells scored={o['cells_scored']} rules={o['rules_scored']} "
          f"tp={o['tp']} fp={o['fp']} fn={o['fn']} tn={o['tn']}")
    print(f"overall precision={o['precision']:.3f} recall={o['recall']:.3f} f1={o['f1']:.3f}")
    if report["below_floor"]:
        print("BELOW FLOOR:")
        for b in report["below_floor"]:
            print(f"  {b['rule_id']} precision={b['precision']} recall={b['recall']}")
        return 1
    print("all scored rules meet the floors")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
