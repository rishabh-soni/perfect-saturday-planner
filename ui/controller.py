"""Small, testable boundary between consumer inputs and the Phase 1 backend."""
from __future__ import annotations

import os
import re
from datetime import time
from urllib.parse import urlencode

from agent.planner import Planner, sanitize
from agent.schemas import PlannerResult, Preferences


INTERESTS = ["Food", "Music", "Nature", "Art", "Cafés", "Shopping", "History", "Walks", "Books"]
CONSTRAINTS = ["Vegetarian", "Vegan", "Avoid crowds", "Low walking", "Budget-friendly", "Wheelchair accessible", "No alcohol"]
MOODS = ["Relaxed", "Adventurous", "Romantic", "Energetic", "Tired but want something fun"]
DEFAULTS = {"city": "Bangalore", "neighborhood": "", "budget": 2000,
            "hours": 4, "start": time(11, 0), "mood": "Relaxed",
            "interests": ["Food", "Walks"], "constraints": ["Vegetarian", "Avoid crowds"],
            "extra_constraints": "", "travel_mode": "Walking"}
PRESETS = {
    "An easy daytime plan": DEFAULTS,
    "Art & a little wandering": {**DEFAULTS, "mood": "Relaxed", "interests": ["Art", "Books", "Walks"]},
    "Food, first": {**DEFAULTS, "mood": "Adventurous", "interests": ["Food", "Cafés"], "hours": 3},
    "The assignment example": {**DEFAULTS, "start": time(10, 0), "mood": "tired but wants to do something fun", "interests": ["Food", "Music", "Walks"]},
}


def build_preferences(*, city, neighborhood, budget, hours, start, mood, interests,
                      constraints, extra_constraints, travel_mode) -> Preferences:
    interest_map = {"cafés": "food", "history": "culture", "art": "art"}
    constraint_map = {"avoid crowds": "avoid crowded places"}
    # Budget-friendly is enforced through the supplied numeric budget, not an
    # unsupported hard dietary/accessibility constraint. Low walking remains hard.
    selected = [constraint_map.get(c.casefold(), c.casefold()) for c in constraints if c.casefold() != "budget-friendly"]
    selected += [part.strip().lower() for part in re.split(r"[,;\n]", extra_constraints) if part.strip()]
    return Preferences.model_validate({
        "city": city, "starting_neighborhood": neighborhood.strip() or None,
        "budget": budget, "available_time": f"{hours} hours", "start_time": start.strftime("%H:%M"),
        "mood": mood, "interests": list(dict.fromkeys(interest_map.get(i.casefold(), i.casefold()) for i in interests)),
        "constraints": list(dict.fromkeys(selected)), "travel_mode": travel_mode.casefold(),
    })


def availability() -> dict:
    return {"openai": bool(os.getenv("OPENAI_API_KEY", "").strip()),
            "google": bool(os.getenv("GOOGLE_MAPS_API_KEY", "").strip()),
            "demo_only": os.getenv("SATURDAY_DEMO_ONLY", "").lower() in {"1", "true", "yes"}}


def generate(preferences: Preferences, *, demo=False, on_event=None, planner_factory=Planner) -> PlannerResult:
    settings = availability()
    offline = demo or settings["demo_only"] or not settings["openai"]
    events = []
    def relay(event):
        events.append(event)
        if on_event:
            on_event(event)
    try:
        planner = planner_factory(on_event=relay)
        result = planner.plan(preferences.model_dump(mode="json"), offline=offline)
        return planner.add_alternatives(result) if callable(getattr(planner, "add_alternatives", None)) else result
    except Exception:
        # Never display a raw SDK/HTTP exception or credential-bearing URL.
        return PlannerResult(status="failure", mode="offline_demo" if offline else "agent",
                             message="Something interrupted planning. Please retry, simplify your preferences, or choose the Bengaluru demo.",
                             preferences=preferences.context(), trace=events)


def public_result(result: PlannerResult) -> dict:
    """Redact secrets in ALL UI and download fields, not just trace arguments."""
    data = sanitize(result.model_dump(mode="json"))
    data["trace"] = [sanitize(event.model_dump(mode="json")) for event in result.trace]
    if result.validation:
        data["validation"]["warnings"] = [sanitize(w) for w in result.validation.warnings]
        data["validation"]["errors"] = [sanitize(e) for e in result.validation.errors]
    if result.itinerary:
        data["itinerary"]["warnings"] = [sanitize(w) for w in result.itinerary.get("warnings", [])]
    return data


def selected_result(result: dict, option_id: str | None = None) -> dict:
    """Selection changes presentation/export, never the submitted preferences or trace."""
    option = next((o for o in result.get("alternatives", []) if o["id"] == option_id), None)
    if option is None:
        return result
    return {**result, **{k: option[k] for k in ("status", "message", "itinerary", "validation")},
            "selected_option_id": option["id"]}


def maps_url(venue: dict) -> str | None:
    name, address = venue.get("name"), venue.get("address")
    if not name:
        return None
    if venue.get("source") == "openstreetmap":
        match = re.fullmatch(r"osm:(node|way|relation):(\d+)", venue.get("place_id", ""))
        if match:
            return f"https://www.openstreetmap.org/{match[1]}/{match[2]}"
    params = {"api": "1", "query": ", ".join(filter(None, [name, address]))}
    if venue.get("source") == "google_places":
        if venue.get("place_id"):
            params["query_place_id"] = venue["place_id"]
        elif venue.get("latitude") is not None and venue.get("longitude") is not None:
            params["query"] = f"{venue['latitude']},{venue['longitude']}"
    # Mock coordinates are approximate: use an honest name/address search instead.
    return "https://www.google.com/maps/search/?" + urlencode(params)


def budget_categories(itinerary: dict) -> list[dict]:
    totals = {category: {"Category": category, "Upper estimate (INR)": 0.0, "unknown": False,
                         "confidence": "verified", "present": False} for category in ["Food", "Activities", "Transportation", "Other"]}
    pairs = [("Food" if s.get("kind") == "food" else "Other" if s.get("kind") == "other" else "Activities", s.get("cost"))
             for s in itinerary.get("stops", [])]
    pairs.append(("Transportation", itinerary.get("transport_cost")))
    for category, price in pairs:
        row = totals[category]
        row["present"] = True
        if not price or price.get("maximum") is None or price.get("confidence") == "unknown":
            row["unknown"] = True
            row["confidence"] = "unknown"
        else:
            row["Upper estimate (INR)"] += price["maximum"]
            if price.get("confidence") != "verified" and row["confidence"] != "unknown":
                row["confidence"] = "estimated"
    return list(totals.values())


EVENT_LABELS = {
    "resolve_city": "Locating your city",
    "resolve_starting_neighborhood": "Locating your starting area",
    "get_weather": "Checking Saturday weather",
    "compare_options": "Comparing your options",
    "simplify_plan": "Checking a shorter plan",
    "infeasibility_review": "Trying a simpler Saturday",
    "planning_budget": "Preparing your final plan",
    "parse_preferences": "Understanding your preferences",
    "search_places": "Finding places that fit",
    "get_route": "Checking the journey",
    "estimate_cost": "Adding up the budget",
    "validate_plan": "Checking time and constraints",
    "validation_revision": "Adjusting the plan",
    "relax_soft_preferences": "Trying a broader search",
    "model_response": "Putting your Saturday together",
}


def event_label(name: str) -> str:
    return EVENT_LABELS.get(name, "Planning update")
