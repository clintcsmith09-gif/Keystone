"""Cost gate — deterministic, config-driven LLM cost control (architecture §11.3).

Phase 0.6: every audit run is gated against a per-audit token budget AND a
monthly aggregate budget before and during execution:

  * pre-run:   a deterministic upper-bound estimate of tokens (from file size +
               row count + rule-set size) decides whether the run may start.
  * mid-flight: after each stage, the *measured* tokens decide whether to halt
               at the next stage boundary (partial artifacts preserved).
  * monthly:   aggregate spend across audits this month is compared to a cap;
               at 80% the owner is warned, at the cap new audits queue for the
               owner review queue (status awaiting_owner_approval).

The gate is pure and deterministic — it never calls an LLM and never spends
money. It computes on a token budget by default and can be extended to dollars
when a provider + price are configured (Phase 1), via cost_gate_price_*.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from .rules import RuleSet


@dataclass(frozen=True)
class Estimate:
    tokens_in: int
    tokens_out: int


@dataclass(frozen=True)
class Decision:
    action: str            # "pass" | "halt" | "warn" | "block" | "approved"
    tokens_in: int = 0
    tokens_out: int = 0
    budget_in: int = 0
    budget_out: int = 0
    reason: str = ""


def _judgment_rule_count(rule_set: RuleSet) -> int:
    """Only judgment / llm_assist rules incur LLM tokens (§7.3); deterministic
    checks (presence/threshold/format/reference) never touch the LLM."""
    return sum(
        1 for r in rule_set.rules
        if r.check_type == "judgment" or r.llm_assist
    )


def _row_estimate(settings: Settings, file_size_bytes: int) -> int:
    """Deterministic upper-bound row count from file size, capped (§11.3)."""
    if file_size_bytes <= 0:
        return 1
    bpr = max(1, settings.cost_gate_estimate_bytes_per_row)
    return min(settings.cost_gate_estimate_row_cap, max(1, file_size_bytes // bpr))


def estimate_tokens(
    settings: Settings,
    rule_set: RuleSet,
    file_size_bytes: int,
    row_count: int | None = None,
) -> Estimate:
    """Deterministic upper-bound token estimate for a pre-run gate (§11.3)."""
    rows = row_count if row_count is not None else _row_estimate(settings, file_size_bytes)
    judgment = _judgment_rule_count(rule_set)
    safety = max(1.0, settings.cost_gate_estimate_safety_factor)
    est_in = (
        rows * settings.cost_gate_estimate_tokens_per_row
        + file_size_bytes * settings.cost_gate_estimate_tokens_per_byte
        + judgment * settings.cost_gate_estimate_tokens_per_judgment_rule
    ) * safety
    est_out = (
        judgment * settings.cost_gate_estimate_tokens_out_per_judgment_rule
    ) * safety
    return Estimate(tokens_in=int(est_in), tokens_out=int(est_out))


def pre_run_decision(
    settings: Settings,
    rule_set: RuleSet,
    file_size_bytes: int,
    row_count: int | None = None,
) -> Decision:
    """Does the run's upper-bound estimate fit inside the per-audit budget?"""
    if not settings.cost_gate_enabled:
        return Decision("pass")
    est = estimate_tokens(settings, rule_set, file_size_bytes, row_count)
    if est.tokens_in > settings.cost_gate_max_tokens_in or \
       est.tokens_out > settings.cost_gate_max_tokens_out:
        return Decision(
            "halt", est.tokens_in, est.tokens_out,
            settings.cost_gate_max_tokens_in, settings.cost_gate_max_tokens_out,
            f"pre-run estimate ({est.tokens_in} in / {est.tokens_out} out) exceeds "
            f"per-audit budget ({settings.cost_gate_max_tokens_in} in / "
            f"{settings.cost_gate_max_tokens_out} out)",
        )
    return Decision("pass", est.tokens_in, est.tokens_out,
                    settings.cost_gate_max_tokens_in, settings.cost_gate_max_tokens_out)


def mid_flight_decision(
    settings: Settings,
    measured_tokens_in: int,
    measured_tokens_out: int,
) -> Decision:
    """Has the run already burned more than the per-audit token budget?"""
    if not settings.cost_gate_enabled:
        return Decision("pass")
    if measured_tokens_in > settings.cost_gate_max_tokens_in or \
       measured_tokens_out > settings.cost_gate_max_tokens_out:
        return Decision(
            "halt", measured_tokens_in, measured_tokens_out,
            settings.cost_gate_max_tokens_in, settings.cost_gate_max_tokens_out,
            f"measured ({measured_tokens_in} in / {measured_tokens_out} out) exceeds "
            f"per-audit budget ({settings.cost_gate_max_tokens_in} in / "
            f"{settings.cost_gate_max_tokens_out} out)",
        )
    return Decision("pass", measured_tokens_in, measured_tokens_out,
                    settings.cost_gate_max_tokens_in, settings.cost_gate_max_tokens_out)


def monthly_token_caps(settings: Settings) -> tuple[int, int]:
    """Monthly cap in tokens (in, out). Default: explicit token budgets. If a real
    provider price + monthly revenue are configured, derive from ratio of revenue
    (the §11.3 '20% of monthly revenue' dollar extension)."""
    if (
        settings.monthly_revenue_usd > 0
        and settings.cost_gate_price_per_million_in > 0
    ):
        pin = settings.cost_gate_price_per_million_in / 1_000_000.0
        pout = (settings.cost_gate_price_per_million_out / 1_000_000.0)
        if pout <= 0:
            pout = pin
        if pin > 0:
            dollar_cap = settings.monthly_revenue_usd * settings.cost_gate_monthly_cap_ratio
            return int(dollar_cap / pin), int(dollar_cap / pout)
    return settings.cost_gate_monthly_tokens_in, settings.cost_gate_monthly_tokens_out


def monthly_decision(
    settings: Settings,
    used_in: int,
    used_out: int,
) -> Decision:
    """Monthly aggregate check. Returns:
      * "warn"  — >= 80% of the monthly cap consumed (owner notified).
      * "block" — cap reached: new audits must queue for the owner review queue.
      * "pass"  — headroom remains.
    """
    if not settings.cost_gate_enabled:
        return Decision("pass")
    cap_in, cap_out = monthly_token_caps(settings)
    warn_in = cap_in * settings.cost_gate_monthly_warn_ratio
    warn_out = cap_out * settings.cost_gate_monthly_warn_ratio
    blocked = used_in >= cap_in or used_out >= cap_out
    warned = used_in >= warn_in or used_out >= warn_out
    if blocked:
        return Decision(
            "block", used_in, used_out, cap_in, cap_out,
            f"monthly budget reached ({used_in} in / {used_out} out of "
            f"{cap_in}/{cap_out}); new audits queue for owner approval",
        )
    if warned:
        return Decision(
            "warn", used_in, used_out, cap_in, cap_out,
            f"monthly budget >= {int(settings.cost_gate_monthly_warn_ratio*100)}% "
            f"consumed ({used_in} in / {used_out} out of {cap_in}/{cap_out})",
        )
    return Decision("pass", used_in, used_out, cap_in, cap_out)


def estimated_cost_usd(
    settings: Settings,
    tokens_in: int,
    tokens_out: int,
) -> float | None:
    """Monetary cost of a token count under the configured provider price. Returns
    None when no price is configured (gate stays purely on token budget)."""
    pin = settings.cost_gate_price_per_million_in
    pout = settings.cost_gate_price_per_million_out
    if pin <= 0 and pout <= 0:
        return None
    return tokens_in * (pin / 1_000_000.0) + tokens_out * (pout / 1_000_000.0)
