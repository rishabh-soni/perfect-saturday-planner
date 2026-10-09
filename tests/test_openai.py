import httpx
import pytest
from openai import OpenAI

from agent.planner import Planner
from main import EXAMPLE
from providers.openai import OpenAIModel, BASE_URL


@pytest.mark.parametrize("status,code,phrase", [
    (429, "insufficient_quota", "credits or quota"),
    (429, "credit_balance_exhausted", "credits or quota"),
    (429, "rate_limit_exceeded", "rate-limited"),
    (401, "invalid_api_key", "API key"), (403, "forbidden", "denied access"),
    (404, "model_not_found", "model is unavailable"), (400, "invalid_request", "rejected"),
    (503, "unavailable", "temporarily unavailable"), (504, "deadline", "deadline")])
def test_openai_error_mapping_and_safe_trace(status, code, phrase, monkeypatch):
    monkeypatch.setenv("OPENAI_DIAGNOSTICS", "0")
    monkeypatch.setenv("GROQ_DIAGNOSTICS", "0")
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"code": code, "message": "private-secret"}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with OpenAI(api_key="test-key", base_url=BASE_URL, max_retries=0, http_client=transport) as client:
            result = Planner(client=client, places_mode="mock").plan(EXAMPLE)
    assert result.status == "failure" and phrase in result.message
    assert "private-secret" not in result.model_dump_json()
    assert result.trace[-1].arguments["provider"] == "OpenAI"
    assert len(requests) == 1


def test_openai_endpoint_ignores_old_groq_settings(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1")
    provider = OpenAIModel(api_key="test-key")
    try:
        assert str(provider.client.base_url).rstrip("/") == BASE_URL
        assert provider.client.max_retries == 0
        assert provider.model == "gpt-4.1-mini"
    finally:
        provider.close()


def test_planner_uses_openai_model_override(monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-20b")
    assert Planner().model == "gpt-4.1-mini"
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4.1")
    assert Planner().model == "gpt-4.1"


def test_final_phase_forces_submit_plan_and_preserves_validation():
    import json
    from agent.schemas import Decision, Itinerary
    from tests.test_validation import plan
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        decision = Decision(status="recommendation", message="Short outing", itinerary=Itinerary.model_validate(plan()))
        return httpx.Response(200, json={"id": "chat-final", "object": "chat.completion", "created": 0,
            "model": "gpt-4.1-mini", "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{"id": "submission", "type": "function",
                    "function": {"name": "submit_plan", "arguments": decision.model_dump_json()}}]}}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with OpenAI(api_key="test-key", http_client=transport) as client:
            from agent.planner import TOOL_MODELS, DESCRIPTIONS
            session = OpenAIModel(client=client).start("Plan safely", EXAMPLE, TOOL_MODELS, DESCRIPTIONS)
            session.finalize()
            turn = session.next_turn()
            session.feedback("Independent validation rejected the draft; correct it.")
            session.next_turn()
    assert turn.decision and turn.decision.itinerary
    assert all([t["function"]["name"] for t in body["tools"]] == ["submit_plan"] for body in seen)
    assert all(body["tools"][0]["function"]["strict"] is True for body in seen)
    schema = seen[0]["tools"][0]["function"]["parameters"]
    assert schema["$defs"]["Stop"]["properties"]["cost"] == {"$ref": "#/$defs/Price"}
    assert all(body["tool_choice"] == {"type": "function", "function": {"name": "submit_plan"}} for body in seen)
    assert any(m["role"] == "tool" and m["tool_call_id"] == "submission" for m in seen[1]["messages"])
