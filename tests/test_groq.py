import json
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI
from openai.types.chat import ChatCompletion
from providers.groq import BASE_URL

from agent.planner import Planner, TOOL_MODELS, DESCRIPTIONS
from agent.prompts import SYSTEM_PROMPT
from main import EXAMPLE
from providers.groq import GroqModel
from providers.llm import ModelError, ModelProtocolError
from tests.test_agent import planner_with, search_step, budget_step, final
from tests.test_validation import plan


def function_call(name, args, identifier="final"):
    return {"type": "function", "id": identifier, "function": {"name": name, "arguments": json.dumps(args)}}


def session_with(calls, content=None):
    response = ChatCompletion.model_validate({"id": "chat-test", "object": "chat.completion", "created": 0,
        "model": "openai/gpt-oss-20b", "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": content, "reasoning": "private hidden reasoning", "tool_calls": calls}}]})
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response)))
    return GroqModel(client=client).start(SYSTEM_PROMPT, EXAMPLE, TOOL_MODELS, DESCRIPTIONS)


def sdk_client(handler, api_key="test-key"):
    return OpenAI(api_key=api_key, base_url=BASE_URL, max_retries=0,
                  http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_malformed_submission_gets_function_response_and_can_retry():
    session = session_with([function_call("submit_plan", {"status": "bad"})])
    turn = session.next_turn()
    assert turn.correction and turn.decision is None
    session.feedback(turn.correction)
    response = session.history[-1]
    assert response["name"] == "submit_plan" and response["tool_call_id"] == "final"
    assert "error" in json.loads(response["content"])
    assert session.pending == []


def test_parallel_calls_defer_final_submission_and_match_each_result():
    from providers.llm import ToolResult
    session = session_with([function_call(name, {}, str(i))
                            for i, name in enumerate(["search_places", "submit_plan", "estimate_cost"])])
    turn = session.next_turn()
    assert [c.name for c in turn.calls] == ["search_places", "estimate_cost"]
    session.respond([ToolResult(c, {"ok": c.name}) for c in turn.calls])
    responses = session.history[-3:]
    assert [r["tool_call_id"] for r in responses] == ["0", "1", "2"]
    assert "error" in json.loads(responses[1]["content"])
    assert json.loads(responses[2]["content"])["result"]["ok"] == "estimate_cost"


def test_mismatched_function_result_is_rejected():
    session = session_with([function_call("search_places", {})])
    session.next_turn()
    with pytest.raises(ModelProtocolError):
        session.respond([])


@pytest.mark.parametrize("code,phrase", [(429, "quota"), (401, "API key"), (403, "denied access"),
                                        (404, "model is unavailable"), (400, "rejected"), (503, "temporarily unavailable")])
def test_real_sdk_errors_are_safe_and_do_not_retry(code, phrase):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(code, json={"error": {"code": code, "message": "raw-private-secret", "status": "ERROR"}})
    with sdk_client(handler) as client:
        result = Planner(llm=GroqModel(client=client), places_mode="mock").plan(EXAMPLE)
    assert result.status == "failure" and phrase in result.message
    assert "raw-private-secret" not in result.model_dump_json()
    assert len(calls) == 1
    assert result.trace[-1].success is False


def test_structured_correction_preserves_bounded_planner_loop():
    from providers.llm import ModelTurn
    planner, provider = planner_with([search_step(), budget_step(), lambda _: ModelTurn(correction="Fix schema"), final(plan())])
    result = planner.plan(EXAMPLE)
    assert result.status == "conditional" and result.iterations == 4
    assert any(e.summary.startswith("Invalid structured") for e in result.trace)


def test_groq_default_and_override(monkeypatch):
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    assert GroqModel(api_key="test-key").model == "openai/gpt-oss-20b"
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-120b")
    assert GroqModel(api_key="test-key", model="openai/gpt-oss-120b").model == "openai/gpt-oss-120b"


def test_diagnostics_redact_credentials_and_exclude_hidden_content(monkeypatch):
    monkeypatch.setenv("GROQ_DIAGNOSTICS", "1")
    monkeypatch.setenv("GROQ_API_KEY", "private-test-credential")
    from providers.diagnostics import redact
    assert redact({"upstream_message": "Rejected private-test-credential", "thought_signature": "hidden", "api_key": "hidden"}) == {
        "upstream_message": "Rejected [REDACTED]", "thought_signature": "[REDACTED]", "api_key": "[REDACTED]"}
    session = session_with([function_call("submit_plan", {"status": "bad"})])
    turn = session.next_turn()
    encoded = json.dumps(turn.diagnostics)
    assert "private hidden reasoning" not in encoded
    assert turn.diagnostics["schema_errors"]
    assert all("input" not in e for e in turn.diagnostics["schema_errors"])
    assert "status" in turn.correction


def test_api_diagnostic_contains_status_and_redacted_message(monkeypatch):
    monkeypatch.setenv("GROQ_DIAGNOSTICS", "1")
    monkeypatch.setenv("GROQ_API_KEY", "private-test-credential")
    def handler(request):
        return httpx.Response(503, json={"error": {"message": "Overloaded private-test-credential", "code": "UNAVAILABLE"}})
    with sdk_client(handler, "private-test-credential") as client:
        result = Planner(llm=GroqModel(client=client), places_mode="mock").plan(EXAMPLE)
    diagnostic = result.trace[-1].arguments
    assert diagnostic["http_status"] == 503
    assert diagnostic["api_status"] == "UNAVAILABLE"
    assert diagnostic["upstream_message"] == "Overloaded [REDACTED]"
    assert "HTTP 503" in result.trace[-1].summary
    assert "private-test-credential" not in result.model_dump_json()


def test_non_function_response_requests_correction_without_logging_reasoning(monkeypatch):
    monkeypatch.setenv("GROQ_DIAGNOSTICS", "1")
    session = session_with([], content="Unstructured answer")
    turn = session.next_turn()
    assert turn.correction and not turn.decision
    assert "private hidden reasoning" not in json.dumps(turn.diagnostics)
    session.feedback(turn.correction)
    assert session.history[-1]["role"] == "user"


def test_deadline_failure_is_explicit():
    from providers.groq import api_error_message
    assert "deadline (HTTP 504)" in api_error_message(SimpleNamespace(status_code=504))


@pytest.mark.parametrize("arguments", ["{bad", "[]", "null"])
def test_malformed_json_calls_receive_matching_error_response(arguments):
    call = function_call("search_places", {})
    call["function"]["arguments"] = arguments
    session = session_with([call])
    turn = session.next_turn()
    assert turn.correction and not turn.calls
    session.feedback(turn.correction)
    assert session.history[-1]["tool_call_id"] == call["id"]
    assert "error" in json.loads(session.history[-1]["content"])


@pytest.mark.parametrize("identifiers", [["", "b"], ["same", "same"]])
def test_missing_or_duplicate_ids_are_rejected(identifiers):
    session = session_with([function_call("search_places", {}, identifier) for identifier in identifiers])
    with pytest.raises(ModelProtocolError, match="call IDs"):
        session.next_turn()


def test_tool_result_with_wrong_id_is_rejected():
    from providers.llm import ToolRequest, ToolResult
    session = session_with([function_call("search_places", {}, "expected")])
    session.next_turn()
    with pytest.raises(ModelProtocolError):
        session.respond([ToolResult(ToolRequest("search_places", {}, "wrong"), {})])


def test_owned_client_uses_groq_endpoint_and_no_automatic_retries(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid")
    provider = GroqModel(api_key="test-key")
    try:
        assert str(provider.client.base_url).rstrip("/") == BASE_URL
        assert provider.client.max_retries == 0
        assert provider.client.timeout == 30
    finally:
        provider.close()


def test_invalid_tool_generation_error_does_not_leak_reasoning(monkeypatch):
    monkeypatch.setenv("GROQ_DIAGNOSTICS", "1")
    def handler(request):
        return httpx.Response(400, json={"error": {"code": "tool_use_failed", "message": "Invalid tool call",
                    "failed_generation": "private hidden reasoning"}})
    with sdk_client(handler) as client:
        result = Planner(llm=GroqModel(client=client), places_mode="mock").plan(EXAMPLE)
    assert result.status == "failure"
    assert result.trace[-1].arguments["api_status"] == "tool_use_failed"
    assert "private hidden reasoning" not in result.model_dump_json()
