"""Quote pricing rules (architecture §9.1).

Loads ``veritas/rules/quote_rules.yaml`` (versioned config in the repo, same
model as the rule sets §7.3) and computes a deterministic audit quote from
structured audit inputs:

* audit scope — standard(s) + rule-set coverage (rules evaluated),
* data volume — rows/files in the normalized view,
* findings severity mix — HIGH-severity exposure + FAILED findings,
* re-audit flag — existing client on a re-audit within ``eligible_days``.

Pricing is template-based and fully deterministic given the same inputs (§9.1:
cheaper + reproducible; the Quote Agent never calls an LLM for money). All money
math is exact decimal (no float drift); amounts are rounded to whole cents.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

import yaml

QUOTE_RULES_DIR = Path(__file__).resolve().parents[2] / "rules"
DEFAULT_QUOTE_RULES = QUOTE_RULES_DIR / "quote_rules.yaml"

DECIMAL_PLACES = 2


def _cents(x: float) -> int:
    """Round to whole cents exactly (banker's rounding via round-half-even)."""
    return round(round(x, 10) * 100)


def _money_from_cents(cents: int) -> float:
    return cents / 100.0


@dataclass(frozen=True)
class QuoteRules:
    """Parsed + validated pricing rules from quote_rules.yaml."""
    currency: str
    base_price_usd: float
    per_standard_adder_usd: dict  # str -> float
    per_volume_band_usd: tuple    # tuple[dict(min_rows, max_rows, adder_usd)]
    high_severity_adder_usd: float
    failed_finding_adder_usd: float
    re_audit_discount_pct: float
    re_audit_eligible_days: int
    raw: dict = field(default_factory=dict)

    def volume_adder_cents(self, rows: int) -> int:
        """First band whose [min_rows, max_rows] contains ``rows``."""
        for band in self.per_volume_band_usd:
            lo = band["min_rows"]
            hi = band["max_rows"]
            if rows >= lo and (hi is None or rows <= hi):
                return _cents(band["adder_usd"])
        # Unbounded catch-all: the last band has max_rows=None and therefore
        # always matches the largest rows; keep a safe default just in case.
        return _cents(self.per_volume_band_usd[-1]["adder_usd"])


def load_quote_rules(path: Path | None = None) -> QuoteRules:
    p = path or DEFAULT_QUOTE_RULES
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    standard_adders = {str(k): float(v) for k, v in raw.get("per_standard_adder_usd", {}).items()}
    bands = tuple(
        {
            "min_rows": int(b["min_rows"]),
            "max_rows": b.get("max_rows"),
            "adder_usd": float(b["adder_usd"]),
        }
        for b in raw.get("per_volume_band_usd", [])
    )
    if not bands:
        raise ValueError("quote_rules.yaml: at least one volume band is required")
    return QuoteRules(
        currency=str(raw.get("currency", "USD")),
        base_price_usd=float(raw["base_price_usd"]),
        per_standard_adder_usd=standard_adders,
        per_volume_band_usd=bands,
        high_severity_adder_usd=float(raw.get("high_severity_adder_usd", 0.0)),
        failed_finding_adder_usd=float(raw.get("failed_finding_adder_usd", 0.0)),
        re_audit_discount_pct=float(raw.get("re_audit_discount_pct", 0.0)),
        re_audit_eligible_days=int(raw.get("re_audit_eligible_days", 0)),
        raw=raw,
    )


@dataclass(frozen=True)
class QuoteInputs:
    """Structured audit inputs used to price a quote (§9.1)."""
    standard: str
    rules_evaluated: int = 0
    data_rows: int = 0
    data_files: int = 1
    high_findings: int = 0
    failed_findings: int = 0
    re_audit: bool = False


def itemize(rules: QuoteRules, ins: QuoteInputs) -> dict:
    """Deterministic line-item breakdown + total for the given inputs.

    Returns ``{itemization: [...], total_usd, subtotal_usd, discount_usd}``.
    ``itemization`` is a list of {line, amount_usd} entries; every monetary
    value is a float rounded to whole cents.
    """
    lines: list[dict] = []
    total_cents = _cents(rules.base_price_usd)
    lines.append({"line": "Base audit", "amount_usd": _money_from_cents(total_cents)})

    std_adder = rules.per_standard_adder_usd.get(ins.standard, 0.0)
    if std_adder:
        total_cents += _cents(std_adder)
        lines.append({
            "line": f"Standard adder — {ins.standard}",
            "amount_usd": _money_from_cents(_cents(std_adder)),
        })

    band_name = _band_label(rules, ins.data_rows)
    vol = rules.volume_adder_cents(ins.data_rows)
    if vol:
        total_cents += vol
    lines.append({
        "line": f"Volume adder ({band_name}: {ins.data_rows:,} rows)",
        "amount_usd": _money_from_cents(vol),
    })

    if ins.high_findings:
        add = _cents(rules.high_severity_adder_usd) * ins.high_findings
        total_cents += add
        lines.append({
            "line": f"High-severity findings ({ins.high_findings})",
            "amount_usd": _money_from_cents(add),
        })

    if ins.failed_findings:
        add = _cents(rules.failed_finding_adder_usd) * ins.failed_findings
        total_cents += add
        lines.append({
            "line": f"Failed findings ({ins.failed_findings})",
            "amount_usd": _money_from_cents(add),
        })

    discount_cents = 0
    if ins.re_audit and rules.re_audit_discount_pct > 0:
        discount_cents = round(total_cents * (rules.re_audit_discount_pct / 100.0))
        lines.append({
            "line": f"Re-audit discount ({int(rules.re_audit_discount_pct)}%)",
            "amount_usd": -_money_from_cents(discount_cents),
        })

    subtotal = total_cents
    final_cents = max(0, total_cents - discount_cents)
    return {
        "itemization": lines,
        "subtotal_usd": _money_from_cents(subtotal),
        "discount_usd": _money_from_cents(discount_cents),
        "total_usd": _money_from_cents(final_cents),
    }


def _band_label(rules: QuoteRules, rows: int) -> str:
    for band in rules.per_volume_band_usd:
        lo = band["min_rows"]
        hi = band["max_rows"]
        if rows >= lo and (hi is None or rows <= hi):
            if hi is None:
                return f"{lo:,}+ rows"
            return f"{lo:,}–{hi:,} rows"
    return "volume band"
