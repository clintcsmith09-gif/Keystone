"""Block A — audit-quality regression guard (ground-truth corpus + accuracy floors).

These tests are the reason the harness exists: they run the engine's real
Normalize -> Match stages over the labeled corpus with the OFFLINE noop provider
and fail if per-rule precision or recall drops below the owner-facing floors.
Nothing here touches the network: the anthropic provider is exercised only
through a mocked HTTP transport (like ``test_llm_anthropic.py``) or is proven to
skip cleanly without a key.

Measured baseline (committed artifact ``quality/reports/accuracy-noop-*.json``):
301 dataset x rule cells over 43 deterministic rules — tp 31, fp 0, fn 0,
tn 270, precision/recall/F1 = 1.000, plus row-level 17/0/0.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from anthropic import AsyncAnthropic

from app.audit.llm import AnthropicLLMClient
from app.audit.rules import load_all

from quality import corpus as corpus_mod
from quality import harness

REPORTS_DIR = harness.REPORTS_DIR
TEST_KEY = "sk-ant-test-only-not-real"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return corpus_mod.load_manifest()


@pytest.fixture(scope="module")
def noop_report() -> dict:
    """One noop accuracy run, shared by the tests that need the matrix."""
    return harness.run_accuracy(provider="noop")


def _matrix(report: dict) -> dict:
    return {r["rule_id"]: (r["tp"], r["fp"], r["fn"], r["tn"]) for r in report["per_rule"]}


# --- corpus integrity ----------------------------------------------------------
def test_committed_corpus_matches_the_generator():
    """The corpus is regenerable: committed bytes must equal fresh generator output."""
    assert corpus_mod.verify_corpus() == []


def test_every_label_resolves_to_a_real_rule(manifest):
    assert corpus_mod.validate_expectations(manifest) == []


def test_corpus_manifest_totals_and_labels(manifest):
    files = {f["name"]: f for f in manifest["files"]}
    assert manifest["corpus_version"] == corpus_mod.CORPUS_VERSION
    assert manifest["seed"] == corpus_mod.CORPUS_SEED
    assert len(files) == 7
    # Every labeled failure must be one the deterministic engine can express.
    known = {r.id for rs in load_all().values() for r in rs.rules}
    judgment = {r.id for rs in load_all().values() for r in rs.rules
                if r.check_type == "judgment" or r.llm_assist}
    no_finding_expected = {"clean_ledger.csv", "unmapped_anomalies.csv"}
    for f in manifest["files"]:
        if f["name"] not in no_finding_expected:
            assert f["expected_failures"], f"{f['name']}: dataset carries no labels"
        for rid in f["expected_failures"]:
            assert rid in known
            assert rid not in judgment, (
                f"{f['name']} labels a judgment rule ({rid}); judgment rules always "
                "emit needs_review and are out of scope for ground truth"
            )
        # row labels must point at real rows
        rows = int(f["row_count"])
        for rex in f["row_expectations"]:
            assert 0 <= rex["index"] < rows
    assert manifest["totals"]["files"] == 7
    assert manifest["totals"]["rows"] >= 50
    assert manifest["totals"]["expected_rule_fires"] >= 30


def test_corpus_has_true_negatives_so_precision_is_measurable(manifest):
    files = {f["name"]: f for f in manifest["files"]}
    # Files that must produce NO finding at all: the only way a false positive
    # shows up is if a rule fires where the labels say nothing is wrong.
    assert files["clean_ledger.csv"]["expected_failures"] == []
    assert files["unmapped_anomalies.csv"]["expected_failures"] == []
    # ...and the clean file really is the full schema, so all rules are exercised.
    assert len(files["clean_ledger.csv"]["columns"]) >= 20


def test_uncovered_anomalies_are_reported_not_hidden(manifest):
    """Anomaly classes with no scoped rule are labeled + explained (recall gap)."""
    unmapped = next(f for f in manifest["files"] if f["name"] == "unmapped_anomalies.csv")
    assert len(unmapped["uncovered_anomalies"]) >= 5
    classes = {u["class"] for u in unmapped["uncovered_anomalies"]}
    assert {"off_hours_privileged_login", "failed_login_spike",
            "mfa_gap_privileged_account"} <= classes
    gap_classes = {g["class"] for g in manifest["coverage_gaps"]}
    assert classes <= gap_classes
    assert all(g["why"] for g in manifest["coverage_gaps"])


# --- accuracy floors ----------------------------------------------------------
def test_floors_are_the_agreed_owner_bar():
    # A guard so the floors cannot be silently relaxed to make a build pass.
    assert harness.FLOORS == {"precision": 0.95, "recall": 0.95}


def test_noop_accuracy_meets_the_floors(noop_report):
    o = noop_report["overall"]
    floors = noop_report["floors"]
    assert noop_report["provider"] == "noop"
    # Non-vacuous: the matrix must actually cover the whole deterministic rule set.
    assert o["cells_scored"] >= 300
    assert o["rules_scored"] >= 40
    assert o["tp"] >= 30, "no labeled anomaly was detected — the guard is meaningless"
    assert o["precision"] >= floors["precision"]
    assert o["recall"] >= floors["recall"]
    assert o["f1"] >= floors["precision"]
    # Per rule, wherever the metric is defined.
    for r in noop_report["per_rule"]:
        if r["precision"] is not None:
            assert r["precision"] >= floors["precision"], f"{r['rule_id']} precision"
        if r["recall"] is not None:
            assert r["recall"] >= floors["recall"], f"{r['rule_id']} recall"
    assert noop_report["below_floor"] == []


def test_noop_accuracy_has_no_false_positives_or_negatives(noop_report):
    """Stricter than the floors: this records the measured exact baseline.

    A compliance product emitting a confident wrong finding is the failure mode
    this product cannot afford, so the suite pins fp=fn=0 on the labeled corpus.
    """
    o = noop_report["overall"]
    assert (o["fp"], o["fn"]) == (0, 0)
    rl = noop_report["row_level"]
    # Row-level precision matters too: a rule firing on the wrong ROWS is bad evidence.
    assert (rl["totals"]["fp"], rl["totals"]["fn"]) == (0, 0)
    assert rl["totals"]["tp"] >= 17
    assert not any(r["truncated"] for r in rl["per_rule"])


def test_every_deterministic_rule_is_in_the_matrix(noop_report):
    deterministic = {r.id for rs in load_all().values() for r in rs.rules
                     if r.check_type != "judgment" and not r.llm_assist}
    assert {r["rule_id"] for r in noop_report["per_rule"]} == deterministic


def test_judgment_rules_are_recorded_but_out_of_scope(noop_report):
    judgment = {r.id for rs in load_all().values() for r in rs.rules
                if r.check_type == "judgment" or r.llm_assist}
    scored = {r["rule_id"] for r in noop_report["per_rule"]}
    assert not (judgment & scored), "judgment rules must never be scored"
    reported = {j["rule_id"] for j in noop_report["judgments"]}
    assert reported == judgment
    # They are review requests, never findings: no ground truth exists for them.
    assert all(j["status"] == "needs_review" for j in noop_report["judgments"])


def test_committed_noop_artifact_matches_a_fresh_run(noop_report):
    """The evidence artifact in quality/reports/ must not go stale."""
    artifacts = sorted(REPORTS_DIR.glob("accuracy-noop-*.json"))
    if not artifacts:
        pytest.skip("no committed accuracy artifact")
    committed = json.loads(artifacts[-1].read_text(encoding="utf-8"))
    assert _matrix(committed) == _matrix(noop_report)
    assert committed["overall"] == noop_report["overall"]
    assert committed["corpus"]["seed"] == noop_report["corpus"]["seed"]
    assert committed["corpus"]["integrity_problems"] == []


# --- real provider path (no live calls) ---------------------------------------
def test_anthropic_provider_requires_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("VERITAS_ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(harness.ProviderUnavailable) as exc:
        harness.build_llm("anthropic")
    assert "ANTHROPIC_API_KEY" in str(exc.value)
    # ...and the noop default stays available with no key at all.
    assert harness.build_llm("noop") is not None


def test_anthropic_run_skips_cleanly_without_a_key(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("VERITAS_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("VERITAS_LLM_PROVIDER", "anthropic")
    # The harness reports the skip and exits 2 without making a call.
    assert harness.main(["--provider", "anthropic", "--no-write"]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    # And the same run through the library entry point raises, never silently noops.
    with pytest.raises(harness.ProviderUnavailable):
        harness.run_accuracy(provider="anthropic")


def _mock_transport_client() -> AnthropicLLMClient:
    """Real AnthropicLLMClient wired to a faked HTTP transport (no network)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "msg_test_0001",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": '{"verdict":"review"}'}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 120, "output_tokens": 6},
        })

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler=handler))
    raw = AsyncAnthropic(api_key=TEST_KEY, max_retries=0, http_client=http_client)
    return AnthropicLLMClient(api_key=TEST_KEY, anthropic_client=raw)


def test_same_harness_works_on_the_real_provider_code_path(noop_report):
    """Provider-agnostic proof: identical deterministic matrix through the real
    Anthropic client code, driven by a mocked transport (no network, no key)."""
    llm = _mock_transport_client()
    report = harness.run_accuracy(provider="anthropic", llm=llm)
    assert report["model_id"] == "claude-sonnet-4-5"
    # The judgment seam really ran through the Anthropic code path...
    assert len(report["judgments"]) == len(noop_report["judgments"])
    assert all(j["status"] == "needs_review" for j in report["judgments"])
    # ...and the deterministic accuracy is unchanged, which is the point.
    assert _matrix(report) == _matrix(noop_report)
    assert report["overall"] == noop_report["overall"]
    assert report["below_floor"] == []
