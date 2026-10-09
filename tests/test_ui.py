"""Frontend mapping and real Streamlit rerun tests; never call external APIs."""
import json
from datetime import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from streamlit.testing.v1 import AppTest

from agent.planner import Planner
from agent.schemas import PlannerResult, TraceEvent
from main import EXAMPLE
from ui.components import esc, money, price_label
from ui.controller import (DEFAULTS, availability, budget_categories, build_preferences,
                           generate, maps_url, public_result)


def preferences(**changes):
    return build_preferences(**{**DEFAULTS, **changes})


def test_default_form_maps_to_backend():
    prefs = preferences()
    assert prefs.city == "Bengaluru" and prefs.budget == 2000
    assert prefs.duration_minutes == 240 and prefs.start_time == "11:00"
    assert prefs.interests == ["food", "walks"]
    assert prefs.constraints == ["vegetarian", "avoid crowded places"]
    assert prefs.starting_neighborhood is None and prefs.travel_mode == "walking"


def test_form_matches_assignment_schema():
    prefs = preferences(start=time(10), mood=EXAMPLE["mood"], interests=["Food", "Music", "Walks"])
    expected = Planner().plan(EXAMPLE, offline=True)
    assert prefs.context() == expected.preferences


def test_mapping_extra_constraints_and_interests():
    prefs = preferences(neighborhood="  Indiranagar  ", interests=["Cafés", "Food", "History", "Nature"],
                        constraints=["Avoid crowds", "Budget-friendly", "Low walking"],
                        extra_constraints="vegan, no stairs; no alcohol\nquiet", travel_mode="Driving")
    assert prefs.starting_neighborhood == "Indiranagar"
    assert prefs.interests == ["food", "culture", "nature"]
    assert "budget-friendly" not in prefs.constraints
    assert "low walking" in prefs.hard_constraints
    assert "vegan" in prefs.hard_constraints and "quiet" in prefs.soft_constraints
    assert prefs.travel_mode == "driving"


@pytest.mark.parametrize("overrides", [{"city": " "}, {"mood": ""}, {"interests": []},
                                       {"start": time(23), "hours": 4}, {"budget": -1}])
def test_form_invalid_inputs(overrides):
    with pytest.raises(ValueError):
        preferences(**overrides)


def test_missing_keys_use_actual_backend_demo(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    result = generate(preferences())
    assert result.mode == "offline_demo" and result.status == "conditional"
    assert {"search_places", "get_route", "estimate_cost", "validate_plan"} <= {e.name for e in result.trace}


def test_callback_events_equal_returned_trace():
    events = []
    result = Planner(on_event=events.append).plan(EXAMPLE, offline=True)
    assert [e.model_dump() for e in events] == [e.model_dump() for e in result.trace]
    events[0].summary = "changed outside planner"
    assert result.trace[0].summary != events[0].summary


def test_unexpected_backend_exception_retains_real_events(monkeypatch):
    monkeypatch.setenv("SATURDAY_DEMO_ONLY", "1")
    class BrokenPlanner:
        def __init__(self, on_event):
            self.on_event = on_event
        def plan(self, data, offline):
            self.on_event(TraceEvent(name="search_places", arguments={}, duration_ms=12,
                                     success=False, summary="Provider unavailable", iteration=1))
            raise RuntimeError("credential=private-sensitive-value")
    result = generate(preferences(), planner_factory=BrokenPlanner)
    assert result.status == "failure" and len(result.trace) == 1
    assert "private-sensitive-value" not in result.model_dump_json()


def test_config_exposes_presence_only(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sensitive-key")
    assert availability()["openai"] is True
    assert "sensitive-key" not in json.dumps(availability())


def test_public_download_redacts_secrets_and_keeps_full_trace(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sensitive-key")
    events = [TraceEvent(name="model_response", arguments={}, duration_ms=1,
                         success=True, summary="done", iteration=1) for _ in range(35)]
    result = PlannerResult(status="failure", mode="agent", message="sensitive-key", trace=events)
    public = public_result(result)
    assert public["message"] == "[REDACTED]" and len(public["trace"]) == 35


def test_maps_uses_google_id_but_does_not_treat_mock_coords_as_verified():
    venue = {"name": "A&B café", "address": "Central Bengaluru", "place_id": "real-id",
             "source": "google_places", "latitude": 12, "longitude": 77}
    params = parse_qs(urlparse(maps_url(venue)).query)
    assert params["query_place_id"] == ["real-id"]
    assert params["query"] == ["A&B café, Central Bengaluru"]
    venue["source"] = "mock"
    params = parse_qs(urlparse(maps_url(venue)).query)
    assert "query_place_id" not in params and params["query"] != ["12,77"]
    assert maps_url({}) is None


def test_html_escapes_untrusted_text():
    assert esc('<script>alert("x")</script>') == '&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;'


def test_unknown_budget_not_charted_as_free():
    unknown = {"minimum": None, "maximum": None, "confidence": "unknown", "basis": "unknown"}
    verified = {"minimum": 0, "maximum": 0, "confidence": "verified", "basis": "walking"}
    rows = budget_categories({"stops": [{"kind": "food", "cost": unknown}], "transport_cost": verified})
    food = next(r for r in rows if r["Category"] == "Food")
    transport = next(r for r in rows if r["Category"] == "Transportation")
    assert food["unknown"] and food["confidence"] == "unknown"
    assert transport["present"] and transport["confidence"] == "verified"
    assert money(None) == "Unknown" and price_label(unknown) == "Price unknown"


@pytest.fixture
def app(monkeypatch):
    # Actual local .env can exist: force demo so tests never spend API credits.
    monkeypatch.setenv("SATURDAY_DEMO_ONLY", "1")
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=15).run()
    assert not at.exception
    return at


def test_streamlit_starts_with_useful_defaults(app):
    assert app.text_input(key="city").value == "Bangalore"
    assert app.time_input(key="start").value == time(11)
    assert app.number_input(key="budget").value == 2000
    assert app.toggle(key="demo").value
    assert app.session_state["result"] is None


def submit(app):
    next(b for b in app.button if b.label.startswith("Plan My Saturday")).click().run()
    assert not app.exception


def test_streamlit_submission_and_rerenders_preserve_result(app):
    submit(app)
    result = app.session_state["result"]
    assert result["mode"] == "offline_demo" and result["status"] == "conditional"
    assert result["validation"]["cost"]["total_estimated_cost"] <= 2000
    assert {e["name"] for e in result["trace"]} >= {"search_places", "get_route", "estimate_cost", "validate_plan"}
    assert any("How your Saturday was planned" in e.label for e in app.expander)
    app.checkbox(key="trace_arguments").check().run()
    assert not app.exception
    assert app.session_state["result"] == result
    assert len(app.json) > 0


def test_streamlit_invalid_input_does_not_call_agent(app):
    app.multiselect(key="interests").set_value([])
    submit(app)
    assert app.session_state["result"] is None
    assert any("Check your details" in e.value for e in app.error)


def test_streamlit_unknown_city_graceful_failure(app):
    app.text_input(key="city").input("Delhi")
    submit(app)
    result = app.session_state["result"]
    assert result["status"] == "infeasible"
    assert len(app.error) > 0
    assert sum(e["name"] == "search_places" for e in result["trace"]) == 2


def test_streamlit_infeasible_draft_not_recommendation(app):
    app.number_input(key="budget").set_value(0)
    app.selectbox(key="travel_mode").select("Driving")
    submit(app)
    assert app.session_state["result"]["status"] == "infeasible"
    assert any("Unapproved draft" in e.label for e in app.expander)
    assert not any('class="activity"' in m.value for m in app.markdown)


def test_streamlit_presets_and_regenerate(app):
    app.selectbox(key="preset").select("The assignment example").run()
    assert app.time_input(key="start").value == time(10)
    submit(app)
    before = app.session_state["result"]
    app.button(key="regenerate").click().run()
    assert not app.exception
    assert app.session_state["result"]["preferences"] == before["preferences"]


def test_options_are_distinct_checked_and_match_callback_trace(monkeypatch):
    monkeypatch.setenv("SATURDAY_DEMO_ONLY", "1")
    events = []
    result = generate(preferences(), on_event=events.append)
    assert len(result.alternatives) == 3
    assert len({tuple(s["place_id"] for s in o.itinerary["stops"]) for o in result.alternatives}) == 3
    for option in result.alternatives:
        assert not option.validation.errors
        assert option.validation.total_minutes <= 240
        assert option.validation.cost["total_estimated_cost"] <= 2000
    assert [e.model_dump() for e in events] == [e.model_dump() for e in result.trace]


def test_streamlit_option_selection_updates_display_and_export(app):
    from ui.controller import selected_result
    submit(app)
    result = app.session_state["result"]
    assert len(result["alternatives"]) == 3
    app.radio(key="selected_option").set_value("option-2").run()
    assert not app.exception
    selected = selected_result(result, "option-2")
    assert selected["itinerary"] == result["alternatives"][1]["itinerary"]
    assert selected["preferences"] == result["preferences"]
    assert selected["trace"] == result["trace"]
    assert any(selected["itinerary"]["title"] in m.value for m in app.markdown)
    assert len(app.table) == 1
    assert any("Budget left after the upper estimate" in t.value for t in app.text)


def test_alternatives_never_relax_budget_or_dietary_conflicts():
    planner = Planner()
    result = planner.plan(preferences(budget=0, travel_mode="Driving").model_dump(mode="json"), offline=True)
    result = planner.add_alternatives(result)
    assert result.status == "infeasible" and not result.alternatives
    planner = Planner()
    result = planner.plan(preferences().model_dump(mode="json"), offline=True)
    for place in planner.catalog.values():
        if "restaurant" in place.types:
            place.evidence["vegetarian"] = False
    # Explicit conflicts in a draft must never become approved food alternatives.
    result.status = "infeasible"
    result = planner.add_alternatives(result)
    assert all(s["kind"] != "food" for o in result.alternatives for s in o.itinerary["stops"])


def test_alternative_waits_for_known_opening_inside_original_window():
    from agent.schemas import Place
    planner = Planner()
    result = planner.plan(preferences().model_dump(mode="json"), offline=True)
    template = next(p for p in planner.catalog.values() if "restaurant" in p.types)
    alternate = template.model_copy(deep=True)
    alternate.place_id = "late-lunch"
    alternate.name = "Later Lunch"
    alternate.opening_hours = {"periods": [{"open": {"day": 6, "hour": 12}, "close": {"day": 6, "hour": 14}}]}
    # Keep only one primary stop so the added venue is evaluated as an alternative.
    result.itinerary["stops"] = [result.itinerary["stops"][0]]
    planner.catalog = {result.itinerary["stops"][0]["place_id"]: planner.catalog[result.itinerary["stops"][0]["place_id"]], alternate.place_id: alternate}
    result = planner.add_alternatives(result)
    late = next(o for o in result.alternatives if o.itinerary["stops"][0]["place_id"] == "late-lunch")
    assert late.itinerary["stops"][0]["start_time"] >= "12:00"
    assert late.validation.total_minutes <= 240 and not late.validation.errors


def test_alternatives_respect_shared_tool_limit():
    planner = Planner()
    result = planner.plan(preferences().model_dump(mode="json"), offline=True)
    planner.tool_calls = planner.MAX_TOOL_CALLS
    result = planner.add_alternatives(result)
    assert planner.tool_calls == planner.MAX_TOOL_CALLS
    assert len(result.alternatives) == 1


def test_unknown_cost_table_keeps_total_unknown(app):
    from tools.budget import estimate_cost
    submit(app)
    result = app.session_state["result"]
    result["alternatives"] = []
    stop = result["itinerary"]["stops"][0]
    stop["cost"] = {"minimum": None, "maximum": None, "confidence": "unknown", "basis": "Menu unavailable"}
    result["validation"]["cost"] = estimate_cost(
        [{"label": s["activity"], "cost": s["cost"]} for s in result["itinerary"]["stops"]],
        result["itinerary"]["transport_cost"], budget=2000)
    app.session_state["result"] = result
    app.run()
    assert not app.exception and len(app.table) == 1
    assert any("Complete total and budget left are unknown" in w.value for w in app.warning)
    assert any("Known costs only" in t.value for t in app.text)
