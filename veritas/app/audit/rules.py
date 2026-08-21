"""Rule sets as versioned data (architecture §7.3).

Rules live in the repo under ``veritas/rules/<standard>.yaml`` as normal versioned
config — not in code and not in the database. Each file declares one standard +
version + an ordered list of rules. Findings always cite rule_id + standard +
standard_version, so a finding is reproducible against the exact rules that
produced it.

Check types (subset of §7.3):
  * presence  — a target table/record must exist and expose required column(s).
  * threshold — a numeric condition over rows (e.g. amount >= 0, row count >= N).
  * format    — values in a column match an expected pattern (regex / allowed set).
  * reference — cross-record/table referential integrity on a key.
  * judgment  — requires an LLM (via the LLMClient seam); produces needs_review.

Deterministic checks (presence/threshold/format/reference) never touch the LLM.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path

# Repo-rooted rules directory: <repo>/veritas/rules (this file is app/audit/rules.py).
RULES_DIR = Path(__file__).resolve().parents[2] / "rules"

CHECK_TYPES = ("presence", "threshold", "format", "reference", "judgment")
SEVERITIES = ("high", "medium", "low", "info")


@dataclass(frozen=True)
class Rule:
    id: str
    category: str
    severity: str
    check_type: str
    description: str
    target: str = "ledger"            # table/record name the rule scans
    params: dict = field(default_factory=dict)
    llm_assist: bool = False
    prompt_template: str | None = None

    def __post_init__(self) -> None:
        if self.check_type not in CHECK_TYPES:
            raise ValueError(f"rule {self.id!r}: unknown check_type {self.check_type!r}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"rule {self.id!r}: unknown severity {self.severity!r}")


@dataclass(frozen=True)
class RuleSet:
    standard: str
    version: int
    rules: tuple[Rule, ...]

    def by_id(self, rule_id: str) -> Rule | None:
        for r in self.rules:
            if r.id == rule_id:
                return r
        return None


def _parse_rule(standard: str, version: int, raw: dict) -> Rule:
    rule_id = str(raw.get("id", "")).strip()
    if not rule_id:
        raise ValueError(f"{standard} v{version}: rule missing 'id'")
    for key in ("category", "severity", "check_type", "target"):
        if key not in raw:
            raise ValueError(f"rule {rule_id!r}: missing required field '{key}'")
    return Rule(
        id=rule_id,
        category=str(raw["category"]),
        severity=str(raw["severity"]),
        check_type=str(raw["check_type"]),
        description=str(raw.get("description", "")),
        target=str(raw.get("target", "ledger")),
        params=dict(raw.get("params") or {}),
        llm_assist=bool(raw.get("llm_assist", False)),
        prompt_template=raw.get("prompt_template"),
    )


def load_ruleset(path: Path) -> RuleSet:
    """Load a single rule-set YAML file."""
    import yaml  # imported lazily: PyYAML is a light dep used only here

    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    standard = str(data.get("standard", "")).strip()
    version = int(data.get("version", 1))
    if not standard:
        raise ValueError(f"{path.name}: rule-set file missing 'standard'")
    raw_rules = data.get("rules") or []
    rules = tuple(_parse_rule(standard, version, r) for r in raw_rules)
    if not rules:
        raise ValueError(f"{standard} v{version}: rule set is empty")
    return RuleSet(standard=standard, version=version, rules=rules)


def load_all(rules_dir: Path | None = None) -> dict[str, RuleSet]:
    """Load every ``*.yaml`` rule set, returning {standard: RuleSet}. If multiple
    files claim the same standard, the highest version wins."""
    rules_dir = rules_dir or RULES_DIR
    by_standard: dict[str, RuleSet] = {}
    for path in sorted(rules_dir.glob("*.yaml")):
        rs = load_ruleset(path)
        cur = by_standard.get(rs.standard)
        if cur is None or rs.version > cur.version:
            by_standard[rs.standard] = rs
    return by_standard


def get_ruleset(standard: str, rules_dir: Path | None = None) -> RuleSet | None:
    return load_all(rules_dir).get(standard)


# --- deterministic rule "registerable" helpers shared by the matcher -----------
def safe_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def row_status(rule: Rule, failed: bool, message: str, evidence: dict) -> dict:
    """Normalized matcher result for a single rule."""
    base = {
        "rule_id": rule.id,
        "category": rule.category,
        "severity": rule.severity,
        "status": "failed" if failed else "passed",
        "evidence": evidence,
        "recommendation": None,
    }
    if message:
        base["recommendation"] = message
    return base
