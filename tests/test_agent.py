import json
from types import SimpleNamespace

import httpx
import pytest

from agent.planner import Planner, sanitize, tool_definitions
from agent.schemas import Decision, Itinerary, Preferences
from main import EXAMPLE
from tests.test_validation import plan, price, stop


class ScriptedResponses:
    """API double: validates protocol while choosing tool calls per scripted turn."""
    def __init__(self, steps):
        self.steps = iter(steps)
        self.requests = []

    def parse(self, **kwargs):
        self.requests.append(list(kwargs["input"]))
        return next(self.steps)(kwargs)


def call(name, args, identifier):
    item = SimpleNamespace(type="function_call", name=name, arguments=json.dumps(args), call_id=identifier)
    return lambda _: SimpleNamespace(output=[item], output_parsed=None)


def final(draft):
    return lambda _: SimpleNamespace(output=[], output_parsed=Decision(
        status="recommendation", message="Short nearby stops suit your tired mood.", itinerary=Itinerary.model_validate(draft)))


def planner_with(steps):
    responses = ScriptedResponses(steps)
    return Planner(client=SimpleNamespace(responses=responses), places_mode="mock"), responses


def search_step():
    return call("search_places", {"query": "park food", "city": "Bengaluru", "area": None, "limit": 8}, "search-1")


def budget_step():
    return call("estimate_cost", {"activities": [{"label": "Park", "cost": price()}], "transport_cost": price(confidence="verified")}, "budget-1")


def test_offline_sample_is_personalized_and_traced():
    result = Planner().plan(EXAMPLE, offline=True)
    assert result.status == "conditional" and result.mode == "offline_demo"
    assert not result.validation.passed and not result.validation.errors
    names = {event.name for event in result.trace}
    assert {"search_places", "get_route", "estimate_cost", "validate_plan"} <= names
    assert 2 <= len(result.itinerary["stops"]) <= 3
    assert result.validation.cost["total_estimated_cost"] <= 2000
    assert result.validation.total_minutes < 240
    assert all(s["venue"]["source"] == "mock" for s in result.itinerary["stops"])
    assert any("headphones" in s["activity"] for s in result.itinerary["stops"])
    assert all(event.duration_ms >= 0 for event in result.trace)
    json.loads(result.model_dump_json())


def test_offline_low_budget_time_revises_or_returns_infeasible():
    result = Planner().plan({**EXAMPLE, "budget": 0, "available_time": "20 minutes"}, offline=True)
    assert result.status == "infeasible"
    assert result.validation.status == "invalid"
    assert result.revisions <= 2


def test_offline_unknown_city_relaxes_once():
    result = Planner().plan({**EXAMPLE, "city": "Delhi"}, offline=True)
    assert result.status == "infeasible"
    assert sum(e.name == "search_places" for e in result.trace) == 2
    assert sum(e.name == "relax_soft_preferences" for e in result.trace) == 1


def test_live_agent_empty_search_has_one_server_retry():
    infeasible = lambda _: SimpleNamespace(output=[], output_parsed=Decision(
        status="infeasible", message="No mock venues in this city", itinerary=None))
    planner, responses = planner_with([search_step(), infeasible])
    result = planner.plan({**EXAMPLE, "city": "Delhi"})
    assert result.status == "infeasible"
    assert sum(e.name == "search_places" for e in result.trace) == 2
    assert sum(e.name == "relax_soft_preferences" for e in result.trace) == 1
    tool_output = next(i for i in responses.requests[1] if isinstance(i, dict) and i.get("type") == "function_call_output")
    assert json.loads(tool_output["output"])["status"] == "empty"


def test_failed_tool_arguments_do_not_count_toward_three_tools():
    planner, _ = planner_with([search_step(), call("estimate_cost", {}, "invalid-cost"), final(plan()),
                              budget_step(), final(plan())])
    result = planner.plan(EXAMPLE)
    assert result.status == "conditional" and result.iterations == 5


def test_offline_starting_neighborhood():
    result = Planner().plan({**EXAMPLE, "starting_neighborhood": "central Bengaluru"}, offline=True)
    assert result.status == "conditional"
    assert result.itinerary["starting_place_id"]
    assert not result.validation.errors


def test_missing_openai_key_is_explicit(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = Planner().plan(EXAMPLE)
    assert result.status == "failure"
    assert "OPENAI_API_KEY is missing" in result.message
    assert result.mode == "agent"


def test_agent_correct_call_output_protocol_and_independent_validation():
    planner, responses = planner_with([search_step(), budget_step(), final(plan())])
    result = planner.plan(EXAMPLE)
    assert result.status == "conditional"
    outputs = [x for x in responses.requests[1] if isinstance(x, dict) and x.get("type") == "function_call_output"]
    assert len(outputs) == 1 and outputs[0]["call_id"] == "search-1"
    assert json.loads(outputs[0]["output"])["places"]
    assert result.trace[-1].name == "validate_plan"
    assert result.iterations == 3


def test_invalid_plan_is_revised():
    planner, _ = planner_with([search_step(), budget_step(), final(plan([stop(end="15:00")])), final(plan())])
    result = planner.plan(EXAMPLE)
    assert result.status == "conditional" and result.revisions == 1
    assert any(e.name == "validation_revision" for e in result.trace)


def test_only_two_revisions_allowed():
    bad = final(plan([stop(end="15:00")]))
    planner, _ = planner_with([search_step(), budget_step(), bad, bad, bad])
    result = planner.plan(EXAMPLE)
    assert result.status == "infeasible" and result.revisions == 2


def test_six_iteration_limit():
    planner, _ = planner_with([search_step()] * 6)
    result = planner.plan(EXAMPLE)
    assert result.status == "failure" and result.iterations == 6
    assert "limit" in result.message


def test_bad_arguments_get_safe_tool_failure():
    planner, _ = planner_with([call("get_route", {"api_key": "private-secret"}, "bad")] + [search_step()] * 5)
    result = planner.plan(EXAMPLE)
    failure = next(e for e in result.trace if e.name == "get_route")
    assert not failure.success
    assert "private-secret" not in result.model_dump_json()


def test_original_preferences_cannot_be_overridden_by_tool():
    planner, _ = planner_with([search_step(), budget_step(),
        call("validate_plan", {"itinerary": plan([stop(cost=price(0, 2500))]),
                              "preferences": Preferences.model_validate({**EXAMPLE, "budget": 999999}).model_dump(mode="json")}, "v-1"),
        final(plan())])
    result = planner.plan(EXAMPLE)
    attempted = next(e for e in result.trace if e.name == "validate_plan")
    assert attempted.arguments["preferences"]["budget"] == 2000
    assert "invalid" in attempted.summary


def test_api_exception_does_not_expose_secret():
    def failure(_):
        raise RuntimeError("API key private-secret")
    planner, _ = planner_with([failure])
    result = planner.plan(EXAMPLE)
    assert result.status == "failure" and "private-secret" not in result.model_dump_json()


def test_input_failure_and_planner_reuse_reset():
    planner = Planner()
    planner.plan(EXAMPLE, offline=True)
    result = planner.plan({"city": "Delhi"}, offline=True)
    assert result.status == "failure" and result.preferences is None
    assert len(result.trace) == 1


def test_trace_redacts_nested_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "arbitrary-secret")
    assert sanitize({"api_key": "x", "nested": ["sk-secret123", "arbitrary-secret"]}) == {
        "api_key": "[REDACTED]", "nested": ["[REDACTED]", "[REDACTED]"]}


def test_strict_function_schemas():
    definitions = tool_definitions()
    assert len(definitions) == 4
    assert all(d["type"] == "function" and d["strict"] for d in definitions)
    assert all(d["parameters"]["additionalProperties"] is False for d in definitions)


def test_real_sdk_responses_parse_with_mock_http_transport():
    """Exercises actual SDK request serialization/parsing without an API key/network."""
    from openai import OpenAI
    seen = []
    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        output = [{"type": "function_call", "id": "fc_search", "call_id": "call_search", "name": "search_places",
                   "arguments": json.dumps({"query": "park", "city": "Bengaluru", "area": None, "limit": 8}), "status": "completed"}] if len(seen) == 1 else (
                  [{"type": "function_call", "id": "fc_cost", "call_id": "call_cost", "name": "estimate_cost",
                    "arguments": json.dumps({"activities": [{"label": "Park", "cost": price()}], "transport_cost": price(confidence="verified")}), "status": "completed"}] if len(seen) == 2 else
                  [{"type": "message", "id": "msg_final", "role": "assistant", "status": "completed", "content": [
                      {"type": "output_text", "text": Decision(status="recommendation", message="Enjoy a short stroll", itinerary=Itinerary.model_validate(plan())).model_dump_json(), "annotations": []}]}])
        return httpx.Response(200, json={"id": f"resp_{len(seen)}", "object": "response", "created_at": 0,
            "status": "completed", "error": None, "incomplete_details": None, "instructions": None,
            "model": "gpt-4.1-mini", "output": output, "parallel_tool_calls": False, "tool_choice": "auto",
            "tools": body["tools"], "temperature": 1, "top_p": 1, "usage": None})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAI(api_key="test-not-real", http_client=http_client)
        result = Planner(client=client, places_mode="mock").plan(EXAMPLE)
    assert result.status == "conditional", result.model_dump_json()
    assert seen[0]["text"]["format"]["type"] == "json_schema"
    assert seen[0]["text"]["format"]["strict"] is True
    assert any(item.get("call_id") == "call_search" and item["type"] == "function_call_output" for item in seen[1]["input"])
