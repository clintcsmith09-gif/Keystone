"""Stage 2 — Rule Engine / Compliance Matcher (architecture §7.1, §7.3).

Applies a loaded RuleSet against the normalized single-table view. Every
deterministic check type (presence / threshold / format / reference) runs here
with no LLM; only check_type='judgment' (or llm_assist=True) rules consult the
LLMClient seam, and the result is recorded as needs_review with llm_judgment.

Outcome: a list of per-rule match results (status) that the orchestrator persists
to the findings table with full evidence; the matcher itself is pure (no DB).
"""
from __future__ import annotations
import re
from typing import Callable

from .llm import LLMClient
from .rules import Rule, RuleSet
from .rules import safe_int

MATCH_TEMPLATE_ID = "match-judgment-v1"
AGENT = "rule_engine"

_MAX_VIOLATIONS = 20  # bound evidence; no huge JSONB rows


def _nil(rule: Rule) -> dict:
    """Non-applicable/empty result (column absent but allowed)."""
    return {
        "rule_id": rule.id,
        "category": rule.category,
        "severity": rule.severity,
        "status": "passed",
        "evidence": {"applicable": False, "note": "rule not applicable to this dataset"},
        "recommendation": None,
        "llm_judgment": None,
    }


def _fail(rule: Rule, evidence: dict, rec: str | None = None) -> dict:
    return {
        "rule_id": rule.id,
        "category": rule.category,
        "severity": rule.severity,
        "status": "failed",
        "evidence": evidence,
        "recommendation": rec,
        "llm_judgment": None,
    }


def _pass(rule: Rule, evidence: dict) -> dict:
    return {
        "rule_id": rule.id,
        "category": rule.category,
        "severity": rule.severity,
        "status": "passed",
        "evidence": evidence,
        "recommendation": None,
        "llm_judgment": None,
    }


def _need_review(rule: Rule, judgment: dict) -> dict:
    return {
        "rule_id": rule.id,
        "category": rule.category,
        "severity": rule.severity,
        "status": "needs_review",
        "evidence": {"judgment": True},
        "recommendation": "Requires human/LLM-assisted review",
        "llm_judgment": judgment,
    }


# --- deterministic checkers ---------------------------------------------------
def _check_presence(rule: Rule, view: dict) -> dict:
    columns = set(view.get("columns", []))
    required = [str(c) for c in rule.params.get("columns", [])]
    require = str(rule.params.get("require", "present")).lower()
    allow_missing = bool(rule.params.get("allow_missing", False))
    if require == "absent":
        present = [c for c in required if c in columns]
        if present:
            return _fail(
                rule,
                {"present_columns": present},
                "Sensitive or prohibited fields must not be stored",
            )
        return _pass(rule, {"present_columns": []})
    # require present
    missing = [c for c in required if c not in columns]
    if missing:
        if allow_missing:
            return _nil(rule)
        return _fail(rule, {"missing_columns": missing}, "Required columns missing")
    return _pass(rule, {"present_columns": required})


def _num(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compare(a: float | None, op: str, b: float) -> bool:
    if a is None:
        return False
    ops = {
        "ge": lambda: a >= b,
        "le": lambda: a <= b,
        "gt": lambda: a > b,
        "lt": lambda: a < b,
        "eq": lambda: a == b,
        "ne": lambda: a != b,
    }
    try:
        return ops[op]()
    except KeyError:
        return False


def _check_threshold(rule: Rule, view: dict) -> dict:
    rows = view.get("rows", [])
    column = str(rule.params.get("column", ""))
    op = str(rule.params.get("op", "ge"))
    per_row = bool(rule.params.get("per_row", False))
    aggregate = str(rule.params.get("aggregate", ""))
    value = rule.params.get("value", 0)
    allow_missing = bool(rule.params.get("allow_missing", False))

    if not per_row and aggregate == "row_count":
        n = len(rows)
        ok = _compare(float(n), op, float(safe_int(value)))
        return _pass(rule, {"row_count": n}) if ok else _fail(
            rule, {"row_count": n}, f"row count {n} fails {op} {value}"
        )

    if column not in view.get("columns", []):
        return _nil(rule) if allow_missing else _fail(
            rule, {"missing_columns": [column]}, "Threshold column missing"
        )

    violations: list[int] = []
    if isinstance(value, str):
        # string domain (e.g. op='ne', value='' → every row must be non-empty)
        for i, row in enumerate(rows):
            s = str(row.get(column)) if row.get(column) is not None else ""
            if op == "ne" and s == value:
                violations.append(i)
            elif op == "eq" and s != value:
                violations.append(i)
    else:
        # numeric domain (e.g. op='ge', value=0 → amounts non-negative)
        tval = float(value)
        for i, row in enumerate(rows):
            if not _compare(_num(row.get(column)), op, tval):
                violations.append(i)

    if violations:
        return _fail(
            rule,
            {"column": column, "violations": violations[: _MAX_VIOLATIONS], "count": len(violations)},
            f"{len(violations)} row(s) violate {column} {op} {value}",
        )
    return _pass(rule, {"column": column, "checked": len(rows)})


def _check_format(rule: Rule, view: dict) -> dict:
    rows = view.get("rows", [])
    column = str(rule.params.get("column", ""))
    pattern = rule.params.get("pattern")
    allowed = rule.params.get("allowed")
    allow_missing = bool(rule.params.get("allow_missing", False))
    if column not in view.get("columns", []):
        return _nil(rule) if allow_missing else _fail(
            rule, {"missing_columns": [column]}, "Format column missing"
        )
    rx = re.compile(pattern) if pattern else None
    violations: list[int] = []
    for i, row in enumerate(rows):
        val = row.get(column)
        if val is None or val == "":
            continue
        sval = str(val)
        if rx is not None and not rx.match(sval):
            violations.append(i)
        elif allowed is not None and sval not in allowed:
            violations.append(i)
    if violations:
        return _fail(rule, {"column": column, "violations": violations[: _MAX_VIOLATIONS], "count": len(violations)},
                     f"{len(violations)} row(s) fail format constraints on {column}")
    return _pass(rule, {"column": column, "checked": len(rows)})


def _check_reference(rule: Rule, view: dict) -> dict:
    rows = view.get("rows", [])
    column = str(rule.params.get("column", ""))
    nullable = bool(rule.params.get("nullable", False))
    if column not in view.get("columns", []):
        return _nil(rule)
    seen: dict[str, int] = {}
    empty = 0
    for i, row in enumerate(rows):
        val = row.get(column)
        if val is None or str(val).strip() == "":
            empty += 1
            continue
        seen.setdefault(str(val), i)
    duplicates = [i for i in seen.values() if sum(1 for r in rows if str(r.get(column)) == str(rows[i].get(column))) > 1]
    # simpler: count occurrences
    counts: dict[str, int] = {}
    for r in rows:
        v = r.get(column)
        if v is not None and str(v).strip() != "":
            counts[str(v)] = counts.get(str(v), 0) + 1
    dups = [v for v, c in counts.items() if c > 1][: _MAX_VIOLATIONS]
    problems: list[str] = []
    if empty and not nullable:
        problems.append(f"{empty} empty value(s)")
    if dups:
        problems.append(f"{len(dups)} duplicate value(s)")
    if problems:
        return _fail(rule, {"column": column, "duplicates": dups, "empty": empty},
                     "; ".join(problems) + f" on {column}")
    return _pass(rule, {"column": column, "unique": len(counts), "rows": len(rows)})


async def _check_judgment(rule: Rule, llm: LLMClient, view: dict) -> dict:
    prompt = (
        f"Rule {rule.id} ({rule.category}): {rule.description}. "
        f"Dataset has {view.get('row_count', 0)} rows, columns {view.get('columns', [])}. "
        "Assess compliance and return a JSON verdict."
    )
    template_id = rule.prompt_template or MATCH_TEMPLATE_ID
    result = await llm.complete(prompt, template_id=template_id)
    return _need_review(rule, {
        "model_id": llm.model_id,
        "model_version": llm.model_version,
        "prompt_template_id": template_id,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "verdict": result.text,
    })


_CHECKERS: dict[str, Callable[..., dict]] = {
    "presence": _check_presence,
    "threshold": _check_threshold,
    "format": _check_format,
    "reference": _check_reference,
}


async def match_view(rule_set: RuleSet, view: dict, llm: LLMClient) -> list[dict]:
    """Match a rule set against a normalized view; returns per-rule results."""
    results: list[dict] = []
    llm_tokens_in = llm_tokens_out = 0
    for rule in rule_set.rules:
        if rule.check_type == "judgment" or rule.llm_assist:
            res = await _check_judgment(rule, llm, view)
            if res.get("llm_judgment"):
                j = res["llm_judgment"]
                llm_tokens_in += j.get("tokens_in", 0)
                llm_tokens_out += j.get("tokens_out", 0)
        else:
            fn = _CHECKERS[rule.check_type]
            res = fn(rule, view)
        results.append(res)
    return results
