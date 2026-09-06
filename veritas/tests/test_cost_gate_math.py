"""Pure, DB-free tests of the cost-gate math (architecture §11.3).

The gate must be deterministic and config-driven: these tests pin the budget /
estimate / monthly-cap arithmetic without touching the database or any LLM.
"""
from __future__ import annotations

from app.config import Settings
from app.audit import cost_gate
from app.audit.rules import Rule, RuleSet


def _ruleset(n_judgment: int = 2) -> RuleSet:
    rules = []
    for i in range(n_judgment):
        rules.append(Rule(
            id=f"j{i}", category="access", severity="high", check_type="judgment",
            description=f"judgment rule {i}", llm_assist=True,
        ))
    for i in range(3):  # deterministic rules never touch the LLM
        rules.append(Rule(
            id=f"p{i}", category="access", severity="medium",
            check_type="presence", description=f"presence rule {i}",
        ))
    return RuleSet(standard="ISO-27001", version=1, rules=tuple(rules))


def _settings(**overrides) -> Settings:
    base = dict(
        database_url="postgresql://localhost/x",
        master_key="x" * 64,
        environment="test",
        cost_gate_enabled=True,
    )
    base.update(overrides)
    return Settings(**base)


def test_estimate_scales_with_file_size_and_judgment_rules():
    rs_small = _ruleset(2)
    rs_big = _ruleset(6)
    e1 = cost_gate.estimate_tokens(_settings(), rs_small, file_size_bytes=1_000)
    e2 = cost_gate.estimate_tokens(_settings(), rs_small, file_size_bytes=10_000_000)
    assert e2.tokens_in > e1.tokens_in
    # out-tokens depend only on judgment-rule count (deterministic rules add none)
    eb = cost_gate.estimate_tokens(_settings(), rs_big, file_size_bytes=1_000)
    assert eb.tokens_out > e1.tokens_out


def test_pre_run_halt_and_pass():
    # Huge file -> estimate over the per-audit in-budget => halt.
    d = cost_gate.pre_run_decision(
        _settings(cost_gate_max_tokens_in=100_000), _ruleset(2),
        file_size_bytes=100_000_000)
    assert d.action == "halt"
    # Tiny file within budget => pass.
    d2 = cost_gate.pre_run_decision(
        _settings(cost_gate_max_tokens_in=100_000), _ruleset(2),
        file_size_bytes=100)
    assert d2.action == "pass"


def test_pre_run_disabled_when_cost_gate_off():
    d = cost_gate.pre_run_decision(
        _settings(cost_gate_enabled=False), _ruleset(2), file_size_bytes=10**12)
    assert d.action == "pass"


def test_mid_flight_halt_and_pass():
    assert cost_gate.mid_flight_decision(
        _settings(cost_gate_max_tokens_in=100), 50, 0).action == "pass"
    assert cost_gate.mid_flight_decision(
        _settings(cost_gate_max_tokens_in=100), 150, 0).action == "halt"
    assert cost_gate.mid_flight_decision(
        _settings(cost_gate_max_tokens_out=10), 0, 20).action == "halt"


def test_monthly_warn_block_pass():
    s = _settings(cost_gate_monthly_tokens_in=1_000, cost_gate_monthly_warn_ratio=0.8)
    assert cost_gate.monthly_decision(s, used_in=100, used_out=0).action == "pass"
    # >= 80% => warn
    assert cost_gate.monthly_decision(s, used_in=800, used_out=0).action == "warn"
    # >= 100% => block
    assert cost_gate.monthly_decision(s, used_in=1000, used_out=0).action == "block"


def test_monthly_token_caps_default_and_revenue_derived():
    # Default: explicit token budgets (no revenue / price configured).
    caps = cost_gate.monthly_token_caps(_settings())
    assert caps == (2_000_000, 600_000)
    # Revenue + price configured => derived from 20% of monthly revenue.
    s = _settings(
        monthly_revenue_usd=10_000,
        cost_gate_price_per_million_in=3.0,
        cost_gate_price_per_million_out=1.0,
    )
    cap_in, cap_out = cost_gate.monthly_token_caps(s)
    # $2000 cap @ $3/M in / $1/M out
    assert cap_in == int(2000 / (3.0 / 1e6))
    assert cap_out == int(2000 / (1.0 / 1e6))


def test_estimated_cost_usd_none_without_price():
    assert cost_gate.estimated_cost_usd(_settings(), 1000, 500) is None
    s = _settings(cost_gate_price_per_million_in=3.0, cost_gate_price_per_million_out=1.0)
    c = cost_gate.estimated_cost_usd(s, 1_000_000, 0)
    assert abs(c - 3.0) < 1e-9
