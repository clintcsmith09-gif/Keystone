"""Anthropic LLM client tests — MOCKED TRANSPORT ONLY.

No live API calls, no real key anywhere: every test drives the real
``anthropic.AsyncAnthropic`` SDK through an ``httpx.MockTransport`` handler,
so the full SDK request/response/error path runs without touching the network.

Covers: the provider seam factory (noop stays the default and never crashes),
missing-key failure mode, model pinning, real token counts from the API usage
object flowing into the §11.3 cost gate, latency telemetry, error mapping
(rate limit / 5xx / auth / timeout / connection), and the matcher wiring.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from anthropic import AsyncAnthropic

from app.audit import cost_gate, matcher
from app.audit.llm import (
    LLMProviderError,
    NoopLLMClient,
    AnthropicLLMClient,
    get_llm,
)
from app.audit.rules import Rule, RuleSet
from app.config import Settings

TEST_KEY = "sk-ant-test-only-not-real"


def _settings(**overrides) -> Settings:
    base = dict(
        database_url="postgresql://localhost/x",
        master_key="x" * 64,
        environment="test",
    )
    base.update(overrides)
    return Settings(**base)


def _message_body(
    text: str = '{"verdict":"pass"}',
    *,
    tokens_in: int = 42,
    tokens_out: int = 7,
    model: str = "claude-sonnet-4-5",
) -> dict:
    return {
        "id": "msg_test_0001",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": tokens_in,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": tokens_out,
        },
    }


def _client_for(handler, *, max_retries: int = 0) -> AnthropicLLMClient:
    """Build an AnthropicLLMClient whose SDK transport is a MockTransport —
    the test seams in at the HTTP layer, i.e. a *faked transport*, never a
    live provider."""
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler=handler))
    raw = AsyncAnthropic(
        api_key=TEST_KEY,
        max_retries=max_retries,
        http_client=http_client,
    )
    return AnthropicLLMClient(api_key=TEST_KEY, anthropic_client=raw)


# --- provider seam / factory ------------------------------------------------
def test_noop_default_never_requires_key():
    # Even with NO anthropic key configured anywhere, the default (noop)
    # provider must construct and run — nothing about the real provider may
    # break the offline path.
    client = get_llm(_settings(llm_provider="noop", anthropic_api_key=""))
    assert isinstance(client, NoopLLMClient)
    result = asyncio.run(client.complete("hello", template_id="t1"))
    assert result.tokens_in == 1


def test_noop_factory_never_constructs_anthropic(monkeypatch):
    # Belt-and-braces: provider=noop must not even look at the anthropic SDK.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = get_llm(_settings(llm_provider="noop"))
    assert isinstance(client, NoopLLMClient)


def test_missing_key_fails_with_clear_message():
    with pytest.raises(LLMProviderError) as exc:
        get_llm(_settings(llm_provider="anthropic", anthropic_api_key=""))
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_factory_wires_configured_model_and_version():
    client = get_llm(_settings(
        llm_provider="anthropic",
        anthropic_api_key=TEST_KEY,
        anthropic_model="claude-sonnet-4-5",
        anthropic_api_version="2023-06-01",
    ))
    assert isinstance(client, AnthropicLLMClient)
    assert client.model_id == "claude-sonnet-4-5"
    assert client.model_version == "2023-06-01"


def test_unknown_provider_is_loud():
    # Seam must keep the door open to a future swap (Bedrock/Azure ZDR) — but
    # an unknown provider is a misconfiguration, never a silent fallback.
    with pytest.raises(ValueError, match="llm_provider"):
        get_llm(_settings(llm_provider="bedrock"))


# --- mocked-transport completion -------------------------------------------
def test_complete_returns_text_and_real_usage_tokens():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(200, json=_message_body(
            text='{"verdict":"review"}', tokens_in=88, tokens_out=13), request=request)

    client = _client_for(handler)
    result = asyncio.run(client.complete("judge this", template_id="iso1-v1"))

    assert result.text == '{"verdict":"review"}'
    assert result.tokens_in == 88          # real input_tokens from the API
    assert result.tokens_out == 13         # real output_tokens from the API
    assert result.latency_ms >= 0          # real measured latency
    # The request that went to (the mocked) provider is the pinned model +
    # plain prompt — no ZDR-defeating or retention-affecting params:
    import json
    sent = json.loads(captured["body"])
    assert sent["model"] == "claude-sonnet-4-5"
    assert sent["messages"] == [{"role": "user", "content": "judge this"}]
    assert "store" not in sent and "metadata" not in sent


def test_cache_tokens_never_undercount(capture=True):
    def handler(request: httpx.Request) -> httpx.Response:
        body = _message_body(tokens_in=10, tokens_out=5)
        body["usage"]["cache_creation_input_tokens"] = 7
        body["usage"]["cache_read_input_tokens"] = 3
        return httpx.Response(200, json=body, request=request)

    client = _client_for(handler)
    result = asyncio.run(client.complete("p", template_id="t"))
    # input = 10 + 7 + 3 = 20 — the cost gate must never undercount, so
    # cache tokens count toward tokens_in.
    assert result.tokens_in == 20
    assert result.tokens_out == 5


# --- cost gate uses the mock's real token counts ----------------------------
def test_cost_gate_math_uses_mock_token_counts():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_message_body(tokens_in=60, tokens_out=10),
                              request=request)

    client = _client_for(handler)
    result = asyncio.run(client.complete("p", template_id="t"))

    # The §11.3 mid-flight gate consumes exactly what the provider reported:
    assert cost_gate.mid_flight_decision(
        _settings(cost_gate_max_tokens_in=50), result.tokens_in, result.tokens_out
    ).action == "halt"
    assert cost_gate.mid_flight_decision(
        _settings(cost_gate_max_tokens_in=100), result.tokens_in, result.tokens_out
    ).action == "pass"
    assert cost_gate.mid_flight_decision(
        _settings(cost_gate_max_tokens_out=5), result.tokens_in, result.tokens_out
    ).action == "halt"
    # Dollar extension uses the same counts under a configured price:
    s = _settings(cost_gate_price_per_million_in=3.0, cost_gate_price_per_million_out=15.0)
    cost = cost_gate.estimated_cost_usd(s, result.tokens_in, result.tokens_out)
    assert cost is not None
    assert abs(cost - (60 * 3e-6 + 10 * 15e-6)) < 1e-12


# --- error mapping: transient provider errors must surface, not go hollow ----
def test_rate_limit_maps_to_retryable_kind():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"type": "error", "error": {"type": "rate_limit_error"}},
                              request=request)

    client = _client_for(handler)
    with pytest.raises(LLMProviderError) as exc:
        asyncio.run(client.complete("p", template_id="t"))
    assert exc.value.kind == "rate_limit"


def test_server_error_maps_to_retryable_kind():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"type": "error", "error": {"type": "api_error"}},
                              request=request)

    client = _client_for(handler)
    with pytest.raises(LLMProviderError) as exc:
        asyncio.run(client.complete("p", template_id="t"))
    assert exc.value.kind == "server_error"


def test_auth_error_names_the_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error"}},
                              request=request)

    client = _client_for(handler)
    with pytest.raises(LLMProviderError) as exc:
        asyncio.run(client.complete("p", template_id="t"))
    assert exc.value.kind == "auth"
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_timeout_and_connection_map_to_retryable_kind():
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated provider timeout")

    client = _client_for(timeout_handler)
    with pytest.raises(LLMProviderError) as exc:
        asyncio.run(client.complete("p", template_id="t"))
    assert exc.value.kind in ("timeout", "connection")


# --- matcher wiring: real telemetry lands in the audit trail ----------------
def test_match_view_records_mock_telemetry_in_llm_judgment():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_message_body(
            text='{"verdict":"review"}', tokens_in=88, tokens_out=13), request=request)

    client = _client_for(handler)
    rule = Rule(
        id="j1", category="access", severity="high", check_type="judgment",
        description="judgment rule", llm_assist=True,
        prompt_template="iso27001-judgment-v1",
    )
    rule_set = RuleSet(standard="ISO-27001", version=1, rules=(rule,))
    view = {"row_count": 10, "columns": ["user", "role"]}

    results = asyncio.run(matcher.match_view(rule_set, view, client))
    judgment = results[0]["llm_judgment"]
    assert judgment["model_id"] == client.model_id          # pinned real model
    assert judgment["model_version"] == client.model_version
    assert judgment["prompt_template_id"] == "iso27001-judgment-v1"
    assert judgment["tokens_in"] == 88                      # provider-measured
    assert judgment["tokens_out"] == 13
    assert judgment["latency_ms"] >= 0
    assert judgment["verdict"] == '{"verdict":"review"}'