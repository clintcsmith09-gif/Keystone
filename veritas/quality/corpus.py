"""Block A — ground-truth **labeled** synthetic audit corpus (deterministic).

WHY THIS EXISTS
---------------
Before Block A the engine's findings were only ever *demonstrated* on synthetic
demo data: nothing measured whether a finding was right or wrong. This module
builds a labeled corpus so accuracy is measurable — every seeded anomaly is
tagged with the exact rule id (from ``veritas/rules/*.yaml``) it must trigger,
and every clean value is a true negative that must NOT trigger anything.

WHAT IS IN IT
-------------
Seven CSV datasets (the same single-table "ledger" shape the pipeline's
Normalize stage emits), deliberately spanning the three ways a rule can fire:

* dataset-scoped rules (presence / row-count / uniqueness) — declared per file
  in ``expected_failures``;
* row-scoped rules (format / per-row threshold) — declared per ROW in
  ``row_expectations``, which is also what makes row-level precision/recall
  measurable (a rule that fires on the *wrong* rows is not correct evidence);
* judgment / ``llm_assist`` rules — deliberately NEVER expected to "fail"; they
  emit ``needs_review`` and are excluded from the accuracy matrix (there is no
  ground truth for "should a human look at this").

Two files carry **no** expected findings at all (``clean_ledger.csv``,
``unmapped_anomalies.csv``) — those are what make precision a real measurement
instead of a guess. ``unmapped_anomalies.csv`` additionally holds anomaly
classes the current scoped rule sets cannot see at all (off-hours privileged
logins, failed-login spikes, MFA gaps on privileged accounts, high-value
outliers, retention excess). They are recorded as ``uncovered_anomalies`` +
``coverage_gaps`` in the manifest: recall for those classes is 0 by design and
is reported, not papered over. Naming the gap is the deliverable.

DETERMINISM
-----------
Fixed ``CORPUS_SEED``, no clock/network/randomness outside the seeded
``random.Random``, stable column order, stable row order. ``manifest.json``
carries a ``sha256`` per file so a stale/hand-edited dataset is detectable.
Regenerate/verify::

    cd veritas && python -m quality.corpus write     # (re)write corpus + manifest
    cd veritas && python -m quality.corpus check     # verify committed == regenerated
    cd veritas && python -m quality.corpus summary   # print the manifest summary

Adding an anomaly means adding a row override plus its expected rule id here;
every expectation is validated against the loaded rule sets, so a typo fails
loudly instead of silently inflating the score.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import random
import sys
from pathlib import Path

CORPUS_VERSION = 1
CORPUS_SEED = 20260924
CORPUS_DIR = Path(__file__).resolve().parent / "corpus"
MANIFEST_FILENAME = "manifest.json"

# Canonical full ledger schema. Every rule in veritas/rules references one of
# these columns, so a file with all of them isolates the anomaly under test.
FULL_COLUMNS: tuple[str, ...] = (
    "transaction_id",
    "account_id",
    "user_id",
    "username",
    "role",
    "access_level",
    "status",
    "timestamp",
    "event_type",
    "source_ip",
    "email",
    "classification",
    "patch_level",
    "backup_time",
    "amount",
    "currency",
    "card_number",
    "masked_pan",
    "expiry",
    "holder_name",
    "mfa_enabled",
)
# Columns PCI-DSS.3.2.2 requires to be ABSENT (sensitive authentication data).
SENSITIVE_COLUMNS: tuple[str, ...] = ("cvv", "ssn")
# Columns removed from the schema in the "required column missing" dataset.
DROPPED_COLUMNS: tuple[str, ...] = (
    "role",
    "status",
    "event_type",
    "masked_pan",
    "holder_name",
)

ALLOWED_ROLES = ("admin", "analyst", "auditor", "compliance", "viewer")


# --- clean-row factory ---------------------------------------------------------
def _pan(rng: random.Random, used: set[str]) -> str:
    """A 16-digit, Luhn-plausible-looking, corpus-unique PAN."""
    while True:
        pan = "4" + "".join(str(rng.randrange(10)) for _ in range(15))
        if pan not in used:
            used.add(pan)
            return pan


class _Factory:
    """Builds deterministic, rule-clean ledger rows. Any field a test wants to
    seed as an anomaly is overwritten on top of one of these."""

    def __init__(self, seed: int = CORPUS_SEED) -> None:
        self.rng = random.Random(seed)
        self.used_pans: set[str] = set()

    def clean(self, i: int) -> dict:
        rng = self.rng
        return {
            "transaction_id": f"TXN-{1000 + i:04d}",
            "account_id": f"ACC-{2000 + i:04d}",
            "user_id": f"usr_{3000 + i}",
            "username": f"user{i:03d}.ops",
            "role": "analyst",
            "access_level": "standard",
            "status": "active",
            "timestamp": f"2026-08-{(i % 27) + 1:02d}T{9 + (i % 8):02d}:{(i * 7) % 60:02d}:00",
            "event_type": "transaction_posted",
            "source_ip": f"10.20.{i % 200:03d}.{20 + (i % 200):03d}",
            "email": f"user{i:03d}@agritech.co.za",
            "classification": "internal",
            "patch_level": "2026.08.1",
            "backup_time": "2026-08-01T02:00:00",
            "amount": f"{round(rng.uniform(50, 2400), 2):.2f}",
            "currency": "ZAR",
            "card_number": _pan(rng, self.used_pans),
            "masked_pan": "411111******1111",
            "expiry": "12/27",
            "holder_name": "A Mokoena",
            "mfa_enabled": "true",
        }

    def many(self, n: int) -> list[dict]:
        return [self.clean(i) for i in range(n)]


# --- datasets ------------------------------------------------------------------
def _clean_ledger(f: _Factory) -> dict:
    """Fully compliant ledger — every deterministic rule must PASS."""
    rows = f.many(12)
    # Span every allow-listed role so the allowlist rule is exercised positively.
    for i, role in enumerate(ALLOWED_ROLES):
        rows[i]["role"] = role
    return {
        "columns": FULL_COLUMNS,
        "rows": rows,
        "description": (
            "All-reference clean ledger: every column present, every value "
            "well-formed. All 43 deterministic rules must pass (precision anchor)."
        ),
        "expected_failures": [],
        "row_expectations": [],
    }


def _identity_anomalies(f: _Factory) -> dict:
    """Seeded identity / access-control / logging anomalies."""
    rows = f.many(14)
    # idx 2/3 — duplicated user_id (registration + unique-id rules)
    rows[2]["user_id"] = "usr_9002"
    rows[3]["user_id"] = "usr_9002"
    # idx 3/4 — duplicated username (provisioning rule)
    rows[3]["username"] = "bob.naidoo"
    rows[4]["username"] = "bob.naidoo"
    # idx 5 — user_id with embedded whitespace (malformed primary identifier)
    rows[5]["user_id"] = " usr_9005"
    # idx 6 — empty user_id (sole-use accountability)
    rows[6]["user_id"] = ""
    # idx 7 — non-ISO timestamp (log correlation)
    rows[7]["timestamp"] = "25/08/2026 10:00"
    # idx 8 — implausible source IP (traffic attribution)
    rows[8]["source_ip"] = "999.1.2"
    # idx 9 — malformed email
    rows[9]["email"] = "ops@"
    # idx 10 — role outside the least-privilege allowlist
    rows[10]["role"] = "accountant"
    # idx 11 — empty required status (access review / leaver identification)
    rows[11]["status"] = ""
    # idx 12/13 — duplicated transaction_id (non-repudiation / attribution)
    rows[12]["transaction_id"] = "TXN-DUPLICATE"
    rows[13]["transaction_id"] = "TXN-DUPLICATE"
    return {
        "columns": FULL_COLUMNS,
        "rows": rows,
        "description": "Identity, access-control and logging anomalies (10 seeded rows).",
        "expected_failures": [
            "ISO-27001.A.9.2.1-user-registration",
            "ISO-27001.A.9.2.2-user-access-provisioning",
            "ISO-27001.integrity.1-non-repudiation",
            "PCI-DSS.8.1.unique-id",
            "PCI-DSS.dq.3.transaction-attribution",
        ],
        "row_expectations": [
            {"index": 5, "expects": ["ISO-27001.A.12.1.1-operating-procedures"],
             "anomaly": "user_id contains whitespace"},
            {"index": 6, "expects": ["ISO-27001.A.9.3.1-sole-use"],
             "anomaly": "empty user_id (no accountable principal)"},
            {"index": 7, "expects": ["ISO-27001.A.12.4.3-clock-sync",
                                     "PCI-DSS.10.3.timestamp-format"],
             "anomaly": "non-ISO-8601 timestamp"},
            {"index": 8, "expects": ["ISO-27001.A.13.1.1-network-controls",
                                     "PCI-DSS.1.3.source-ip"],
             "anomaly": "implausible source_ip"},
            {"index": 9, "expects": ["ISO-27001.scope.2-email-format"],
             "anomaly": "malformed email address"},
            {"index": 10, "expects": ["ISO-27001.scope.3-role-allowed",
                                      "PCI-DSS.7.2.least-privilege"],
             "anomaly": "role outside the least-privilege allowlist"},
            {"index": 11, "expects": ["ISO-27001.scope.1-required-field-empty"],
             "anomaly": "empty required status field"},
        ],
    }


def _missing_columns(f: _Factory) -> dict:
    """Required columns dropped from the schema (dataset-scoped presence rules)."""
    dropped = set(DROPPED_COLUMNS)
    columns = tuple(c for c in FULL_COLUMNS if c not in dropped)
    rows = [{k: v for k, v in r.items() if k not in dropped} for r in f.many(6)]
    return {
        "columns": columns,
        "rows": rows,
        "description": (
            "Ledger missing role/status/event_type/masked_pan/holder_name — "
            "exercises presence rules and the 'column missing' branch of "
            "threshold rules."
        ),
        "expected_failures": [
            "ISO-27001.A.9.2.5-review-user-access",
            "ISO-27001.A.9.2.6-removal-access",
            "ISO-27001.A.16.1.4-detect-incidents",
            "ISO-27001.scope.1-required-field-empty",
            "PCI-DSS.10.2.1.event-type",
            "PCI-DSS.12.3.1.role-assignment",
        ],
        "row_expectations": [],
    }


def _sensitive_columns(f: _Factory) -> dict:
    """Sensitive authentication data present (card-data handling defect)."""
    columns = FULL_COLUMNS + SENSITIVE_COLUMNS
    rows = []
    for i, r in enumerate(f.many(5)):
        r["cvv"] = f"{100 + i}"
        r["ssn"] = f"90010{i}1234"
        rows.append(r)
    return {
        "columns": columns,
        "rows": rows,
        "description": (
            "Clean ledger that additionally stores cvv + ssn columns — exactly "
            "one deterministic rule may fire (precision isolation test)."
        ),
        "expected_failures": ["PCI-DSS.3.2.2.do-not-store-sens-auth"],
        "row_expectations": [],
    }


def _empty_ledger(f: _Factory) -> dict:
    """Correct schema, zero rows (audit-log row-count rules)."""
    return {
        "columns": FULL_COLUMNS,
        "rows": [],
        "description": "Header-only dataset: aggregate row-count rules must fire, nothing else.",
        "expected_failures": [
            "ISO-27001.A.12.4.1-event-logging",
            "PCI-DSS.10.2.audit-log",
        ],
        "row_expectations": [],
    }


def _card_anomalies(f: _Factory) -> dict:
    """Seeded cardholder-data / financial-integrity anomalies."""
    rows = f.many(10)
    # idx 1/2 — PAN not a plain 13-19 digit string (spaced / truncated)
    rows[1]["card_number"] = "4111 1111 1111 1111"
    rows[2]["card_number"] = "4242"
    # idx 3 — empty PAN
    rows[3]["card_number"] = ""
    # idx 4/5 — duplicated PAN (duplication risk)
    rows[4]["card_number"] = "5500005555555555"
    rows[5]["card_number"] = "5500005555555555"
    # idx 6 — malformed expiry
    rows[6]["expiry"] = "08/2"
    # idx 7 — negative amount (refund posted as a credit)
    rows[7]["amount"] = "-45.00"
    # idx 8 — non-ISO currency code
    rows[8]["currency"] = "RAND"
    # idx 9 — empty account reference
    rows[9]["account_id"] = ""
    return {
        "columns": FULL_COLUMNS,
        "rows": rows,
        "description": "Cardholder-data (PAN/expiry) and financial data-quality anomalies.",
        "expected_failures": [
            "PCI-DSS.3.5.pan-format",
            "PCI-DSS.3.5.2.pan-nonempty",
            "PCI-DSS.3.5.3.unique-pan",
            "PCI-DSS.3.6.expiry-format",
            "PCI-DSS.dq.1.amount-nonnegative",
            "PCI-DSS.dq.2.currency-allowed",
            "PCI-DSS.dq.4.no-empty-key",
        ],
        "row_expectations": [
            {"index": 1, "expects": ["PCI-DSS.3.5.pan-format"],
             "anomaly": "PAN stored as free text (grouped digits)"},
            {"index": 2, "expects": ["PCI-DSS.3.5.pan-format"],
             "anomaly": "PAN truncated below 13 digits"},
            {"index": 3, "expects": ["PCI-DSS.3.5.2.pan-nonempty"],
             "anomaly": "empty PAN value"},
            {"index": 6, "expects": ["PCI-DSS.3.6.expiry-format"],
             "anomaly": "malformed card expiry"},
            {"index": 7, "expects": ["PCI-DSS.dq.1.amount-nonnegative"],
             "anomaly": "negative transaction amount"},
            {"index": 8, "expects": ["PCI-DSS.dq.2.currency-allowed"],
             "anomaly": "currency not a 3-letter ISO code"},
            {"index": 9, "expects": ["PCI-DSS.dq.4.no-empty-key"],
             "anomaly": "empty account_id"},
        ],
    }


def _unmapped_anomalies(f: _Factory) -> dict:
    """Real anomaly classes the current scoped rule sets CANNOT detect.

    Every row here is a genuine anomaly an auditor would flag, yet no scoped
    rule maps to it, so the engine must stay silent (any fire is a false
    positive). Recall for these classes is 0 by construction and is reported as
    a coverage gap — the honest Block A finding.
    """
    rows = f.many(8)
    # idx 0 — off-hours privileged access, MFA off
    rows[0].update({"role": "admin", "mfa_enabled": "false",
                    "timestamp": "2026-08-14T02:37:00"})
    # idx 1-3 — failed-login spike (repeated auth failures, one lock-out)
    for i in (1, 2, 3):
        rows[i].update({"event_type": "auth_failed", "status": "locked"})
    # idx 4/5 — high-value outlier transactions inside the ledger
    rows[4]["amount"] = "150000.00"
    rows[5]["amount"] = "118000.00"
    # idx 6 — privileged account with MFA disabled
    rows[6].update({"role": "compliance", "mfa_enabled": "false"})
    # idx 7 — off-hours access from an unusual region
    rows[7].update({"timestamp": "2026-08-15T23:58:00", "source_ip": "203.0.113.77"})
    return {
        "columns": FULL_COLUMNS,
        "rows": rows,
        "description": (
            "Anomaly classes with NO mapped scoped rule (off-hours privileged "
            "logins, failed-login spike, MFA gaps, high-value outliers). Any "
            "finding here would be a false positive."
        ),
        "expected_failures": [],
        "row_expectations": [],
        "uncovered_anomalies": [
            {"index": 0, "class": "off_hours_privileged_login",
             "note": "privileged login at 02:37 outside business hours"},
            {"index": 1, "class": "failed_login_spike", "note": "repeated auth failures"},
            {"index": 2, "class": "failed_login_spike", "note": "repeated auth failures"},
            {"index": 3, "class": "failed_login_spike",
             "note": "repeated auth failures followed by lock-out"},
            {"index": 4, "class": "high_value_outlier", "note": "amount far above baseline"},
            {"index": 5, "class": "high_value_outlier", "note": "amount far above baseline"},
            {"index": 6, "class": "mfa_gap_privileged_account",
             "note": "privileged role with mfa_enabled=false"},
            {"index": 7, "class": "off_hours_foreign_access",
             "note": "out-of-hours access from an unusual address"},
        ],
    }


_BUILDERS: dict[str, tuple[str, object]] = {
    "clean_ledger.csv": ("csv", _clean_ledger),
    "identity_anomalies.csv": ("csv", _identity_anomalies),
    "missing_columns_ledger.csv": ("csv", _missing_columns),
    "sensitive_columns_ledger.csv": ("csv", _sensitive_columns),
    "empty_ledger.csv": ("csv", _empty_ledger),
    "card_anomalies.csv": ("csv", _card_anomalies),
    "unmapped_anomalies.csv": ("csv", _unmapped_anomalies),
}

# Anomaly classes present in the corpus that no scoped rule can detect. Reported
# (never silently dropped): this is the measurable recall ceiling of the current
# rule subset.
COVERAGE_GAP_NOTES = {
    "off_hours_privileged_login":
        "No scoped rule expresses a time-of-day/behavioural condition; would need a "
        "new threshold/behavioural check type.",
    "off_hours_foreign_access":
        "No scoped rule compares access time or origin against a baseline.",
    "failed_login_spike":
        "No scoped rule aggregates failed auth events per user/time window (the "
        "current sets only check that an event_type column exists).",
    "mfa_gap_privileged_account":
        "PCI-DSS.8.2.1 only checks that an mfa_enabled COLUMN exists, and "
        "PCI-DSS.8.2.3 is judgment-assisted (needs_review) — no deterministic rule "
        "asserts mfa_enabled=true for privileged roles.",
    "high_value_outlier":
        "No scoped rule bounds transaction magnitude above (only amount >= 0).",
}


# --- rendering / IO ------------------------------------------------------------
def render_dataset(columns, rows) -> bytes:
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    for r in rows:
        writer.writerow({c: r.get(c, "") for c in columns})
    return buf.getvalue().encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def generate() -> tuple[dict[str, bytes], dict]:
    """Build the whole corpus in memory: ``{filename: bytes}`` + manifest dict."""
    factory = _Factory(CORPUS_SEED)
    files: dict[str, bytes] = {}
    manifest_files: list[dict] = []
    coverage_gaps: list[dict] = []

    for name, (kind, builder) in _BUILDERS.items():
        spec = builder(factory)
        columns, rows = spec["columns"], spec["rows"]
        payload = render_dataset(columns, rows)
        files[name] = payload

        expected = set(spec.get("expected_failures", []))
        row_expectations = spec.get("row_expectations", [])
        for rex in row_expectations:
            expected.update(rex["expects"])
        entry = {
            "name": name,
            "kind": kind,
            "description": spec["description"],
            "columns": list(columns),
            "row_count": len(rows),
            "sha256": _sha256(payload),
            "expected_failures": sorted(expected),
            "dataset_scoped_failures": sorted(set(spec.get("expected_failures", []))),
            "row_expectations": row_expectations,
            "uncovered_anomalies": spec.get("uncovered_anomalies", []),
        }
        manifest_files.append(entry)

        for ua in spec.get("uncovered_anomalies", []):
            coverage_gaps.append({
                "file": name,
                "row_index": ua["index"],
                "class": ua["class"],
                "note": ua["note"],
                "no_mapped_rule": True,
                "why": COVERAGE_GAP_NOTES.get(ua["class"], "no scoped rule covers this class"),
            })

    manifest = {
        "corpus_version": CORPUS_VERSION,
        "seed": CORPUS_SEED,
        "generator": "veritas/quality/corpus.py",
        "note": (
            "Ground-truth labels for the Block A accuracy harness. "
            "expected_failures = rule ids that MUST report status='failed' for this "
            "dataset; everything else must not fire. row_expectations additionally "
            "pin the exact 0-based data row a row-scoped rule must flag."
        ),
        "files": manifest_files,
        "coverage_gaps": coverage_gaps,
        "totals": {
            "files": len(manifest_files),
            "rows": sum(f["row_count"] for f in manifest_files),
            "expected_rule_fires": sum(len(f["expected_failures"]) for f in manifest_files),
            "expected_row_level_fires": sum(
                len(rex["expects"]) for f in manifest_files for rex in f["row_expectations"]
            ),
            "uncovered_anomaly_rows": sum(len(f["uncovered_anomalies"]) for f in manifest_files),
        },
    }
    return files, manifest


def render_manifest(manifest: dict) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=False) + "\n").encode("utf-8")


def write_corpus(corpus_dir: Path | None = None) -> list[str]:
    """(Re)generate the corpus on disk. Returns the paths written."""
    corpus_dir = Path(corpus_dir or CORPUS_DIR)
    corpus_dir.mkdir(parents=True, exist_ok=True)
    files, manifest = generate()
    written: list[str] = []
    for name, payload in files.items():
        path = corpus_dir / name
        path.write_bytes(payload)
        written.append(str(path))
    manifest_path = corpus_dir / MANIFEST_FILENAME
    manifest_path.write_bytes(render_manifest(manifest))
    written.append(str(manifest_path))
    return written


def load_manifest(corpus_dir: Path | None = None) -> dict:
    path = Path(corpus_dir or CORPUS_DIR) / MANIFEST_FILENAME
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def verify_corpus(corpus_dir: Path | None = None) -> list[str]:
    """Compare the committed corpus with what the generator produces now.

    Returns a list of human-readable problems ([] = clean). Detects a stale,
    hand-edited or partially-regenerated corpus — the hash in the manifest is
    the single source of truth for "these bytes are the labeled ones".
    """
    corpus_dir = Path(corpus_dir or CORPUS_DIR)
    problems: list[str] = []
    if not corpus_dir.exists():
        return [f"corpus directory missing: {corpus_dir}"]
    expected_files, expected_manifest = generate()
    for name, payload in expected_files.items():
        path = corpus_dir / name
        if not path.exists():
            problems.append(f"missing corpus file: {name}")
            continue
        actual = path.read_bytes()
        if _sha256(actual) != _sha256(payload):
            problems.append(
                f"{name}: bytes differ from the generator output "
                f"(sha256 {_sha256(actual)[:12]} != {_sha256(payload)[:12]})"
            )
    manifest_path = corpus_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        problems.append(f"missing manifest: {MANIFEST_FILENAME}")
    else:
        try:
            actual_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{MANIFEST_FILENAME}: not valid JSON ({exc})")
        else:
            if actual_manifest != expected_manifest:
                problems.append(
                    f"{MANIFEST_FILENAME}: differs from the generator output "
                    "(regenerate with `python -m quality.corpus write`)"
                )
    extra = sorted(
        p.name for p in corpus_dir.glob("*.csv") if p.name not in expected_files
    )
    for name in extra:
        problems.append(f"unexpected corpus file (not in the generator): {name}")
    return problems


def validate_expectations(manifest: dict) -> list[str]:
    """Every labeled rule id must exist in a loaded rule set (typo guard)."""
    from app.audit.rules import load_all

    known = {r.id for rs in load_all().values() for r in rs.rules}
    problems: list[str] = []
    for f in manifest["files"]:
        for rid in f["expected_failures"]:
            if rid not in known:
                problems.append(f"{f['name']}: expected rule id {rid!r} does not exist")
        for rex in f["row_expectations"]:
            if not rex["expects"]:
                problems.append(f"{f['name']}: row {rex['index']} expects nothing (use uncovered_anomalies)")
            for rid in rex["expects"]:
                if rid not in known:
                    problems.append(f"{f['name']}: row {rex['index']} rule id {rid!r} does not exist")
    return problems


def build() -> tuple[bytes, dict[str, bytes], dict]:
    """Convenience for the harness/tests: (manifest bytes, files, manifest)."""
    files, manifest = generate()
    return render_manifest(manifest), files, manifest


def _summary(manifest: dict) -> str:
    lines = [
        f"corpus v{manifest['corpus_version']} seed={manifest['seed']}",
        f"files={manifest['totals']['files']} rows={manifest['totals']['rows']} "
        f"expected_rule_fires={manifest['totals']['expected_rule_fires']} "
        f"expected_row_fires={manifest['totals']['expected_row_level_fires']} "
        f"uncovered_anomaly_rows={manifest['totals']['uncovered_anomaly_rows']}",
    ]
    for f in manifest["files"]:
        lines.append(
            f"  {f['name']:<28} rows={f['row_count']:<3} "
            f"expected_failures={len(f['expected_failures']):<3} "
            f"row_expectations={len(f['row_expectations'])}"
        )
    lines.append(f"coverage_gaps={len(manifest['coverage_gaps'])}")
    for cls in sorted({g['class'] for g in manifest['coverage_gaps']}):
        lines.append(f"  - {cls}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m quality.corpus",
        description="Block A labeled audit-corpus generator/verifier.",
    )
    parser.add_argument("command", choices=("write", "check", "summary"))
    parser.add_argument("--corpus-dir", default=None)
    args = parser.parse_args(argv)
    corpus_dir = Path(args.corpus_dir) if args.corpus_dir else None

    if args.command == "write":
        for path in write_corpus(corpus_dir):
            print(f"wrote {path}")
        print(_summary(load_manifest(corpus_dir)))
        return 0
    if args.command == "check":
        problems = verify_corpus(corpus_dir) + validate_expectations(load_manifest(corpus_dir))
        for p in problems:
            print(f"PROBLEM: {p}", file=sys.stderr)
        if problems:
            return 1
        print("corpus OK — committed bytes match generator output and all labels resolve")
        return 0
    print(_summary(load_manifest(corpus_dir)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
