import pytest

from agent.schemas import Itinerary, Preferences, Price, Route, clock_minutes
from main import EXAMPLE
from providers.mock import PLACES
from tools.budget import estimate_cost
from tools.validator import opening_check, validate_plan


def price(lo=0, hi=0, confidence="estimated"):
    return {"minimum": lo, "maximum": hi, "confidence": confidence, "basis": "Test allowance"}


def stop(identifier="mock:cubbon", start="10:10", end="10:40", cost=None, kind="walk"):
    return {"place_id": identifier, "activity": "Gentle activity", "kind": kind,
            "start_time": start, "end_time": end, "buffer_minutes": 10,
            "cost": cost or price(), "rationale": "Short outing suits a tired mood"}


def plan(stops=None):
    return {"title": "Test itinerary", "stops": stops or [stop()], "starting_place_id": None,
            "transport_cost": price(confidence="verified"), "warnings": [], "trade_offs": []}


def validate(draft, overrides=None, catalog=None, routes=None):
    prefs = Preferences.model_validate({**EXAMPLE, **(overrides or {})})
    return validate_plan(draft, prefs.model_dump(mode="json"),
                         catalog=catalog if catalog is not None else {p.place_id: p for p in PLACES}, routes=routes)


def test_sample_normalization():
    prefs = Preferences.model_validate(EXAMPLE)
    assert prefs.city == "Bengaluru"
    assert prefs.duration_minutes == 240
    assert prefs.hard_constraints == ["vegetarian"]
    assert prefs.soft_constraints == ["avoid crowded places"]
    assert prefs.saturday.weekday() == 5


@pytest.mark.parametrize("duration,minutes", [("2h 30m", 150), ("1.5 hours", 90), ("90 minutes", 90)])
def test_duration(duration, minutes):
    assert Preferences.model_validate({**EXAMPLE, "available_time": duration}).duration_minutes == minutes


@pytest.mark.parametrize("changes", [{"budget": -1}, {"budget": float("nan")}, {"available_time": "tomorrow"},
    {"available_time": "0 hours"}, {"available_time": "-4 hours"}, {"available_time": "4 hours tomorrow"},
    {"start_time": "25:00"}, {"start_time": "23:00"}, {"saturday": "2026-10-09"}, {"interests": []}, {"city": " "}])
def test_bad_input(changes):
    with pytest.raises(ValueError):
        Preferences.model_validate({**EXAMPLE, **changes})


def test_unknown_prices_are_not_zero():
    result = estimate_cost([{"label": "meal", "cost": price(None, None, "unknown")}], price(), budget=500)
    assert result["total_estimated_cost"] is None
    assert result["remaining_budget"] is None
    assert result["unknown_cost_items"] == ["meal"]
    assert result["known_subtotal_range"]["maximum"] == 0


def test_conservative_cost_math():
    result = estimate_cost([{"label": "meal", "cost": price(100.10, 200.20)}], price(50, 100), budget=500)
    assert result["total_estimated_cost"] == 300.20
    assert result["remaining_budget"] == 199.80


def test_budget_violation_rejected():
    result = validate(plan([stop(cost=price(1900, 2100))]))
    assert result["status"] == "invalid" and not result["passed"]
    assert any("Budget exceeded" in e for e in result["errors"])


def test_time_violation_rejected():
    result = validate(plan([stop(end="14:01")]))
    assert any("Available time exceeded" in e for e in result["errors"])


def test_unknown_hours_and_mock_evidence_are_conditional():
    result = validate(plan())
    assert result["status"] == "conditional" and not result["passed"]
    assert not result["errors"]
    assert any("Opening hours unknown" in w for w in result["warnings"])


def test_missing_venue_rejected():
    assert any("not discovered" in e for e in validate(plan([stop("invented")]))["errors"])


def test_missing_route_rejected():
    draft = plan([stop(), stop("mock:koshys", "11:00", "11:40", kind="food")])
    assert any("Missing usable route" in e for e in validate(draft)["errors"])


def test_chronology_accounts_for_route_and_buffer():
    route = Route(origin_id="mock:cubbon", destination_id="mock:koshys", travel_mode="walking",
                  distance_meters=1000, duration_minutes=20, status="estimated", source="mock", confidence="low", warning="fixture")
    draft = plan([stop(), stop("mock:koshys", "10:50", "11:30", kind="food")])
    result = validate(draft, routes={(route.origin_id, route.destination_id, "walking"): route})
    assert any("travel and buffer" in e for e in result["errors"])


def test_negative_duration_and_overlap_rejected():
    result = validate(plan([stop(start="10:30", end="10:20")]))
    assert any("chronological" in e for e in result["errors"])


def test_dietary_false_rejected_unknown_is_conditional():
    place = PLACES[1].model_copy(deep=True)
    place.evidence = {"vegetarian": False}
    draft = plan([stop(place.place_id, kind="food")])
    result = validate(draft, catalog={place.place_id: place})
    assert any("Hard constraint violated" in e for e in result["errors"])
    place.evidence = {}
    result = validate(draft, catalog={place.place_id: place})
    assert result["status"] == "conditional" and not result["passed"]
    assert any("Hard constraint unverified" in w for w in result["warnings"])


def test_unknown_hard_constraint_never_fully_passes():
    assert any("manual confirmation" in w for w in validate(plan(), {"constraints": ["no stairs"]})["warnings"])


def test_food_kind_cannot_evade_dietary_check():
    result = validate(plan([stop("mock:koshys", kind="relax")]), {"constraints": ["vegan"]})
    assert any("vegan" in w for w in result["warnings"])


def test_hours_closed_and_overnight():
    place = PLACES[0].model_copy(deep=True)
    place.opening_hours = {"periods": [{"open": {"day": 6, "hour": 11}, "close": {"day": 6, "hour": 17}}]}
    assert opening_check(place, 10*60, 11*60) is False
    assert opening_check(place, 11*60, 12*60) is True
    assert any("opening hours" in e for e in validate(plan(), catalog={place.place_id: place})["errors"])
    place.opening_hours = {"periods": [{"open": {"day": 5, "hour": 22}, "close": {"day": 6, "hour": 2}}]}
    assert opening_check(place, 30, 90) is True


def test_24_hours_and_empty_hours():
    place = PLACES[0].model_copy(deep=True)
    place.opening_hours = {"periods": [{"open": {"day": 0, "hour": 0}}]}
    assert opening_check(place, 600, 700) is True
    place.opening_hours = {"periods": []}
    assert opening_check(place, 600, 700) is False


def test_known_evidence_can_pass_with_estimated_cost_warning():
    place = PLACES[0].model_copy(deep=True)
    place.source = "google_places"
    place.opening_hours = {"periods": [{"open": {"day": 0, "hour": 0}}]}
    result = validate(plan(), catalog={place.place_id: place})
    assert result["passed"] and result["status"] == "valid"
    assert any("Estimated price" in w for w in result["warnings"])


def test_unknown_cost_blocks_pass_even_with_known_hours():
    place = PLACES[0].model_copy(deep=True)
    place.source = "google_places"
    place.opening_hours = {"periods": [{"open": {"day": 0, "hour": 0}}]}
    result = validate(plan([stop(cost=price(None, None, "unknown"))]), catalog={place.place_id: place})
    assert not result["passed"] and result["status"] == "conditional"


def test_starting_neighborhood_not_ignored():
    assert any("Starting neighborhood" in e for e in validate(plan(), {"starting_neighborhood": "Whitefield"})["errors"])


def test_driving_free_cost_rejected():
    assert any("cannot silently be zero" in e for e in validate(plan(), {"travel_mode": "driving"})["errors"])


def test_fabricated_verified_prices_rejected():
    result = validate(plan([stop(cost=price(10, 10, "verified"))]))
    assert any("Verified activity price unsupported" in e for e in result["errors"])


def test_first_activity_can_start_after_arrival_buffer():
    draft = plan([stop("mock:koshys", "15:10", "16:00", cost=price(350, 650), kind="food")])
    draft["transport_cost"] = price(150, 300)
    result = validate(draft, {"start_time": "15:00", "travel_mode": "driving"})
    assert not result["errors"] and result["status"] == "conditional"
    assert result["total_minutes"] == 60


def test_first_buffer_error_supplies_actionable_start_time():
    draft = plan([stop(start="15:00", end="15:30")])
    result = validate(draft, {"start_time": "15:00"})
    assert any("earliest activity start is 15:10" in e for e in result["errors"])


def test_shifted_first_activity_still_cannot_exceed_original_deadline():
    draft = plan([stop(start="15:10", end="19:01")])
    result = validate(draft, {"start_time": "15:00"})
    assert any("Available time exceeded" in e for e in result["errors"])
