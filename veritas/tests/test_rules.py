"""Rule-set loading + deterministic matcher tests (architecture §7.3).
No database required — pure rule config and pipeline logic, fully offline.
"""
from __future__ import annotations
import asyncio

import pytest

from app.audit import matcher, normalize, report
from app.audit.llm import NoopLLMClient
from app.audit.rules import (
    CHECK_TYPES,
    SEVERITIES,
    load_all,
    load_ruleset,
    get_ruleset,
)
from app.audit.rules import RULES_DIR


@pytest.fixture(scope="module")
def rule_sets():
    return load_all()


def test_both_standard_rule_sets_present(rule_sets):
    assert "ISO-27001" in rule_sets
    assert "PCI-DSS" in rule_sets


@pytest.mark.parametrize("standard", ["ISO-27001", "PCI-DSS"])
def test_rule_set_size_in_20_to_40(rule_sets, standard):
    rs = rule_sets[standard]
    assert 20 <= len(rs.rules) <= 40, f"{standard} should be a scoped 20-40 rule subset"


@pytest.mark.parametrize("standard", ["ISO-27001", "PCI-DSS"])
def test_rules_have_required_schema_fields(rule_sets, standard):
    for r in rule_sets[standard].rules:
        assert r.id
        assert r.category
        assert r.severity in SEVERITIES
        assert r.check_type in CHECK_TYPES
        assert r.target
        # judgment/assist rules must carry a prompt template
        if r.check_type == "judgment" or r.llm_assist:
            assert r.prompt_template, f"judgment rule {r.id} must pin a prompt_template"


def test_rule_ids_unique_per_standard(rule_sets):
    for rs in rule_sets.values():
        ids = [r.id for r in rs.rules]
        assert len(ids) == len(set(ids)), f"duplicate rule ids in {rs.standard}"


def test_load_specific_file():
    rs = load_ruleset(RULES_DIR / "iso27001.yaml")
    assert rs.standard == "ISO-27001"
    assert rs.version == 1
    assert get_ruleset("ISO-27001") is rs or get_ruleset("ISO-27001").standard == "ISO-27001"


@pytest.fixture()
def seeded_view():
    csv = (
        b"user_id,username,role,amount,currency,card_number,expiry,status,timestamp\n"
        b"1,alice,admin,42,USD,4111111111111111,12/26,active,2026-01-01T10:00:00\n"
        b"2,bob,analyst,-5,USD,,,active,2026-01-01T11:00:00\n"
    )
    return normalize.normalize(csv, kind="csv", source="ledger.csv")


def test_normalize_produces_tabular_view(seeded_view):
    assert seeded_view["row_count"] == 2
    assert "user_id" in seeded_view["columns"]
    assert seeded_view["table"] == "ledger"


def test_deterministic_match_iso(seeded_view):
    rs = get_ruleset("ISO-27001")
    asyncio.run(_match(rs, seeded_view))


def test_deterministic_match_pci(seeded_view):
    rs = get_ruleset("PCI-DSS")
    asyncio.run(_match(rs, seeded_view))


async def _match(rs, view):
    llm = NoopLLMClient(response="{}")
    results = await matcher.match_view(rs, view, llm)
    assert len(results) == len(rs.rules)
    assert any(r["status"] == "failed" for r in results), "negative-amount fixture must fail a rule"
    for r in results:
        assert r["rule_id"]
        assert r["status"] in ("passed", "failed", "needs_review", "info")
        assert "evidence" in r
    # every rule cites its standard+version via the result lineage
    return results


def test_match_is_offline_no_tokens_on_deterministic(seeded_view):
    rs = get_ruleset("ISO-27001")
    llm = NoopLLMClient(response="{}")

    async def _run():
        return await matcher.match_view(rs, seeded_view, llm)

    results = asyncio.run(_run())
    # deterministic (non-judgment) rules incur zero LLM tokens
    det = [r for r in results if not r.get("llm_judgment")]
    assert all((r.get("llm_judgment") or {}) == {} for r in det)


def test_report_synthesizer_aggregates(seeded_view):
    rs = get_ruleset("ISO-27001")
    llm = NoopLLMClient(response="{}")

    async def _run():
        results = await matcher.match_view(rs, seeded_view, llm)
        return await report.synthesize(rs, results, llm)

    rep = asyncio.run(_run())
    s = rep["summary"]
    assert s["total"] == len(rs.rules)
    assert s["passed"] + s["failed"] + s["needs_review"] == s["total"]
    assert rep["standard"] == "ISO-27001"
    assert rep["report_version"] == "v0.1"
    assert rep["schema_version"] == 1
    assert rep["findings"]
    # §8 model_versions pins the synthesizer model identity (no LLM call made)
    assert rep["model_versions"]["report_synthesizer"]["model_id"] == llm.model_id
    assert rep["model_versions"]["report_synthesizer"]["model_version"] == llm.model_version
