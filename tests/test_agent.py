import json
from types import SimpleNamespace

import httpx
import pytest

from agent.planner import Planner, sanitize, TOOL_MODELS, DESCRIPTIONS
from providers.llm import ModelTurn, ToolRequest, ModelError
from providers.groq import declarations, api_error_message
from agent.schemas import Decision, Itinerary, Preferences
from main import EXAMPLE
from tests.test_validation import plan, price, stop


class ScriptedResponses:
    name = "Scripted"
    def __init__(self, steps):
        self.steps = iter(steps)
        self.requests = []
        self.history = []

    def start(self, *args):
        return self

    def next_turn(self):
        self.requests.append(list(self.history))
        return next(self.steps)({})

    def respond(self, results):
        self.history.extend({"type": "function_call_output", "call_id": r.request.call_id,
                             "output": json.dumps(r.output)} for r in results)

    def feedback(self, message):
        self.history.append({"role": "user", "content": message})


def call(name, args, identifier):
    return lambda _: ModelTurn(calls=[ToolRequest(name, args, identifier)])


def final(draft):
    return lambda _: ModelTurn(decision=Decision(
        status="recommendation", message="Short nearby stops suit your tired mood.", itinerary=Itinerary.model_validate(draft)))


def planner_with(steps):
    responses = ScriptedResponses(steps)
    return Planner(llm=responses, places_mode="mock"), responses


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
    infeasible = lambda _: ModelTurn(decision=Decision(
        status="infeasible", message="No mock venues in this city", itinerary=None))
    planner, responses = planner_with([search_step(), infeasible, infeasible])
    result = planner.plan({**EXAMPLE, "city": "Delhi"})
    assert result.status == "failure"
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
    assert result.status == "failure" and result.revisions == 2


def test_configured_six_iteration_limit(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_ITERATIONS", "6")
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


@pytest.mark.parametrize("code,phrase", [(429, "quota"), (403, "denied access"),
                                        (404, "model is unavailable"), (503, "temporarily unavailable")])
def test_billing_failure_shows_actionable_message_without_raw_error(code, phrase):
    def failure(_):
        raise ModelError(api_error_message(SimpleNamespace(status_code=code, message="raw-private-secret")))
    planner, responses = planner_with([failure])
    result = planner.plan(EXAMPLE)
    assert result.status == "failure" and phrase in result.message
    assert "raw-private-secret" not in result.model_dump_json()
    assert len(responses.requests) == 1


def test_trace_redacts_nested_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "arbitrary-secret")
    assert sanitize({"api_key": "x", "nested": ["sk-secret123", "arbitrary-secret"]}) == {
        "api_key": "[REDACTED]", "nested": ["[REDACTED]", "[REDACTED]"]}


def test_strict_function_schemas():
    definitions = declarations(TOOL_MODELS, DESCRIPTIONS)
    assert len(definitions) == 5
    assert definitions[-1]["function"]["name"] == "submit_plan"
    assert all(d["function"]["parameters"]["additionalProperties"] is False for d in definitions)


@pytest.mark.parametrize("backend,base_url,model", [("groq", "https://api.groq.com/openai/v1", "openai/gpt-oss-20b"),
                                                   ("openai", "https://api.openai.com/v1", "gpt-4.1-mini")])
def test_real_sdk_function_calling_with_mock_http_transport(backend, base_url, model):
    """Actual OpenAI SDK against Groq's chat protocol, all four dynamic tools."""
    from openai import OpenAI
    from providers.groq import GroqModel
    from providers.openai import OpenAIModel
    BASE_URL = base_url
    seen = []
    draft = plan()
    def handler(request):
        assert str(request.url) == BASE_URL + "/chat/completions"
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            calls = [("search_places", {"query": "park food", "city": "Bengaluru", "area": None, "limit": 8}, "search")]
        elif len(seen) == 2:
            calls = [("get_route", {"origin": {"place_id": "mock:cubbon", "latitude": None, "longitude": None},
                    "destination": {"place_id": "mock:koshys", "latitude": None, "longitude": None}, "travel_mode": "walking"}, "route"),
                ("estimate_cost", {"activities": [{"label": "Park", "cost": price()}], "transport_cost": price(confidence="verified")}, "cost"),
                ("validate_plan", {"itinerary": draft, "preferences": Preferences.model_validate(EXAMPLE).model_dump(mode="json")}, "validate")]
        else:
            calls = [("submit_plan", Decision(status="recommendation", message="Enjoy a short stroll", itinerary=Itinerary.model_validate(draft)).model_dump(mode="json"), "final")]
        return httpx.Response(200, json={"id": "chat-test", "object": "chat.completion", "created": 0,
            "model": body["model"], "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "reasoning": "hidden reasoning", "tool_calls": [
                {"type": "function", "id": identifier, "function": {"name": name, "arguments": json.dumps(args)}}
                for name, args, identifier in calls]}}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        with OpenAI(api_key="test-not-real", base_url=BASE_URL, max_retries=0, http_client=http_client) as client:
            provider = GroqModel if backend == "groq" else OpenAIModel
            result = Planner(llm=provider(client=client), places_mode="mock").plan(EXAMPLE)
    assert result.status == "conditional", result.model_dump_json()
    assert len(seen) == 3 and seen[0]["model"] == model
    assert seen[0]["tool_choice"] == "required"
    assert len(seen[0]["tools"]) == 5 and "response_format" not in seen[0]
    outputs = [m for m in seen[2]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in outputs] == ["search", "route", "cost", "validate"]
    assert json.loads(outputs[0]["content"])["result"]["places"]
    assert "hidden reasoning" not in json.dumps(seen) + result.model_dump_json()
    assert {"search_places", "get_route", "estimate_cost", "validate_plan"} <= {e.name for e in result.trace}
    assert next(e for e in result.trace if e.name == "get_route").success


def test_agent_can_complete_after_six_turns():
    planner, _ = planner_with([search_step()] * 5 + [budget_step(), final(plan())])
    result = planner.plan(EXAMPLE)
    assert result.status == "conditional" and result.iterations == 7
    assert not result.validation.errors


def test_default_iteration_limit_and_finish_feedback(monkeypatch):
    monkeypatch.delenv("AGENT_MAX_ITERATIONS", raising=False)
    planner, provider = planner_with([search_step()] * 12)
    result = planner.plan(EXAMPLE)
    assert result.status == "failure" and result.iterations == 12
    assert "12-iteration" in result.message
    assert any(e.name == "planning_budget" for e in result.trace)
    assert any("Three model turns remain" in m.get("content", "") for m in provider.history)


@pytest.mark.parametrize("value,expected", [("invalid", 12), ("1000", 20), ("0", 3), ("8", 8)])
def test_iteration_limit_is_configurable_and_bounded(monkeypatch, value, expected):
    monkeypatch.setenv("AGENT_MAX_ITERATIONS", value)
    assert Planner().max_iterations == expected


def test_revision_after_turn_six_is_validated():
    planner, _ = planner_with([search_step()] * 4 + [budget_step(), final(plan([stop(end="15:00")])), final(plan())])
    result = planner.plan(EXAMPLE)
    assert result.status == "conditional" and result.iterations == 7 and result.revisions == 1
    assert not result.validation.errors


def test_planner_reserves_two_final_turns_for_submission(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_ITERATIONS", "8")
    planner, provider = planner_with([search_step()] * 5 + [budget_step(), final(plan())])
    invoked = []
    provider.finalize = lambda: invoked.append(planner.iterations)
    result = planner.plan(EXAMPLE)
    assert result.status == "conditional" and invoked == [7]
    assert any(e.arguments.get("phase") == "final_submission" for e in result.trace)


def test_invalid_tool_arguments_return_safe_field_hints():
    planner, provider = planner_with([call("estimate_cost", {"api_key": "private-secret"}, "bad"), search_step(), budget_step(), final(plan())])
    result = planner.plan(EXAMPLE)
    output = json.loads(provider.history[0]["output"])
    assert output["argument_errors"]
    assert any(e["field"] == "activities" for e in output["argument_errors"])
    assert "private-secret" not in json.dumps(output) + result.model_dump_json()
    assert all("input" not in e for e in output["argument_errors"])


def infeasible_step():
    return lambda _: ModelTurn(decision=Decision(status="infeasible", message="No verified prices or quiet music venues", itinerary=None))


def test_unsubstantiated_infeasibility_is_challenged_once():
    planner, provider = planner_with([search_step(), infeasible_step(), infeasible_step()])
    result = planner.plan(EXAMPLE)
    assert result.status == "failure" and "does not establish" in result.message
    assert sum(e.name == "infeasibility_review" for e in result.trace) == 1
    assert any("Estimated costs are allowed" in m.get("content", "") for m in provider.history)
    assert "No verified prices" not in result.message


def test_afternoon_driving_plan_recovers_from_premature_infeasibility():
    draft = plan([stop("mock:koshys", "15:10", "16:00", cost=price(350, 650), kind="food")])
    draft["transport_cost"] = price(150, 300)
    draft["trade_offs"] = ["Food is prioritized; headphones during seated rest can cover music. A separate walk is optional."]
    planner, provider = planner_with([search_step(), budget_step(), infeasible_step(), final(draft)])
    result = planner.plan({**EXAMPLE, "start_time": "15:00", "travel_mode": "driving"})
    assert result.status == "conditional" and not result.validation.errors
    assert result.validation.total_minutes == 60
    assert result.validation.cost["total_estimated_cost"] == 950
    assert result.preferences["travel_mode"] == "driving"
    assert result.preferences["hard_constraints"] == ["vegetarian"]
    assert any(e.name == "infeasibility_review" for e in result.trace)


def test_unknown_data_and_soft_preferences_do_not_require_full_verification():
    draft = plan([stop("mock:koshys", "15:10", "16:00", cost=price(None, None, "unknown"), kind="food")])
    draft["transport_cost"] = price(150, 300)
    planner, _ = planner_with([search_step(), budget_step(), final(draft)])
    result = planner.plan({**EXAMPLE, "start_time": "15:00", "travel_mode": "driving"})
    assert result.status == "conditional" and not result.validation.errors
    assert result.validation.cost["total_estimated_cost"] is None
    assert any("Crowds" in w for w in result.validation.warnings)


def test_repeated_invalid_drafts_report_search_failure_not_impossibility():
    bad = final(plan([stop(end="15:00")]))
    planner, _ = planner_with([search_step(), budget_step(), bad, bad, bad])
    result = planner.plan(EXAMPLE)
    assert result.status == "failure" and result.validation.errors
    assert "does not establish" in result.message
    assert result.revisions == 2


def test_missing_route_can_recover_one_discovered_food_stop():
    draft = plan([stop("mock:koshys", "15:10", "16:00", price(350, 650), "food"),
                  stop("mock:gallery", "16:20", "17:00", price(20, 100), "culture")])
    draft["transport_cost"] = price(150, 300)
    validation_call = call("validate_plan", {"itinerary": draft,
        "preferences": Preferences.model_validate({**EXAMPLE, "start_time": "15:00", "travel_mode": "driving"}).model_dump(mode="json")}, "v")
    planner, _ = planner_with([search_step(), validation_call, infeasible_step()])
    result = planner.plan({**EXAMPLE, "start_time": "15:00", "travel_mode": "driving"})
    assert result.status == "conditional" and not result.validation.errors
    assert len(result.itinerary["stops"]) == 1
    assert result.itinerary["stops"][0]["place_id"] == "mock:koshys"
    assert result.validation.cost["total_estimated_cost"] == 950
    assert any(e.name == "simplify_plan" for e in result.trace)
    assert {"search_places", "estimate_cost", "validate_plan"} <= planner.executed_tools


def test_simpler_recovery_corrects_first_buffer_without_extending_deadline():
    draft = plan([stop("mock:koshys", "15:00", "15:50", price(350, 650), "food")])
    draft["transport_cost"] = price(150, 300)
    planner, _ = planner_with([search_step(), budget_step(), final(draft), infeasible_step()])
    result = planner.plan({**EXAMPLE, "start_time": "15:00", "travel_mode": "driving"})
    assert result.status == "conditional" and not result.validation.errors
    assert result.itinerary["stops"][0]["start_time"] == "15:10"
    assert result.itinerary["stops"][0]["end_time"] == "16:00"


def test_simpler_recovery_cannot_bypass_dietary_false_evidence():
    draft = plan([stop("mock:koshys", cost=price(350, 650), kind="food")])
    planner, _ = planner_with([search_step(), budget_step(), final(draft), infeasible_step()])
    # Inject a mock fixture with explicit contraindicating evidence.
    from providers.mock import MockProvider
    class UnsafeFood(MockProvider):
        def search(self, *args, **kwargs):
            places = super().search(*args, **kwargs)
            return [p.model_copy(update={"evidence": {"vegetarian": False}}, deep=True) if p.place_id == "mock:koshys" else p for p in places]
    planner.mock = UnsafeFood()
    result = planner.plan(EXAMPLE)
    assert result.status == "failure"
    assert result.validation is None or result.validation.errors
    assert not any(e.name == "validate_plan" and e.success for e in result.trace)


def test_simpler_recovery_cannot_bypass_budget():
    draft = plan([stop("mock:koshys", cost=price(2500, 3000), kind="food")])
    planner, _ = planner_with([search_step(), budget_step(), final(draft), infeasible_step()])
    result = planner.plan(EXAMPLE)
    assert result.status == "failure"
    assert not any(e.name == "validate_plan" and e.success for e in result.trace)


def test_simpler_recovery_does_not_invent_neighborhood_route():
    draft = plan([stop("mock:koshys", cost=price(350, 650), kind="food")])
    planner, _ = planner_with([search_step(), budget_step(), final(draft), infeasible_step(), infeasible_step()])
    result = planner.plan({**EXAMPLE, "starting_neighborhood": "central Bengaluru"})
    assert result.status == "failure"
    assert not any(e.name == "validate_plan" and e.success for e in result.trace)


def test_simpler_recovery_respects_tool_execution_cap():
    planner = Planner()
    planner._reset()
    planner.preferences = Preferences.model_validate(EXAMPLE)
    planner.tool_calls = 23
    assert planner._recover_simpler(Itinerary.model_validate(plan())) is None
    assert planner.tool_calls == 23
