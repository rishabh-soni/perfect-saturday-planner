"""Bounded Responses API agent plus an explicit deterministic offline demo."""
from __future__ import annotations

import json
import math
import os
import re
from time import perf_counter

from pydantic import ValidationError

from agent.prompts import SYSTEM_PROMPT
from agent.schemas import (Decision, Itinerary, Place, PlannerResult, Preferences,
                           Price, Route, Stop, TraceEvent, ValidationResult, clock_minutes, clock_string)
from providers.google import GoogleProvider
from providers.mock import MockProvider
from tools.budget import BudgetArguments, estimate_cost
from tools.places import SearchArguments, search_places
from tools.routing import RouteArguments, get_route
from tools.validator import ValidationArguments, validate_plan


TOOL_MODELS = {"search_places": SearchArguments, "get_route": RouteArguments,
               "estimate_cost": BudgetArguments, "validate_plan": ValidationArguments}
DESCRIPTIONS = {
    "search_places": "Discover venues in the requested city. Missing Google key/failure falls back to labeled Bengaluru mock fixtures.",
    "get_route": "Get walking/driving route between discovered place IDs or their exact coordinates. Failure is unavailable, mock routes are estimates.",
    "estimate_cost": "Calculate INR itemized upper/lower estimated costs against the user's original budget. Unknown costs remain unknown.",
    "validate_plan": "Check chronology, route evidence, time, original budget, hard constraints, Saturday hours and critical unknowns. Original preferences are enforced.",
}


def tool_definitions():
    # Public SDK helper also generates strict nested Pydantic schemas correctly.
    from openai import pydantic_function_tool
    definitions = []
    for name, model in TOOL_MODELS.items():
        function = pydantic_function_tool(model, name=name, description=DESCRIPTIONS[name])["function"]
        definitions.append({"type": "function", **function})
    return definitions


def sanitize(value):
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if re.search(r"key|secret|token|authorization|password", str(k), re.I)
                else sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value[:30]]
    if isinstance(value, str):
        value = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", value)
        value = re.sub(r"AIza[A-Za-z0-9_-]+", "[REDACTED]", value)
        for key in (os.getenv("OPENAI_API_KEY"), os.getenv("GOOGLE_MAPS_API_KEY")):
            if key:
                value = value.replace(key, "[REDACTED]")
        return value[:3000]
    return value


class Planner:
    MAX_ITERATIONS = 6
    MAX_REVISIONS = 2
    MAX_TOOL_CALLS = 24

    def __init__(self, *, client=None, google=None, mock=None, model=None, places_mode="auto"):
        self.client = client
        self.google = google
        self.mock = mock or MockProvider()
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        self.places_mode = places_mode

    def _reset(self):
        self.catalog = {}
        self.routes = {}
        self.trace = []
        self.iterations = self.revisions = self.tool_calls = 0
        self.empty_retry_used = False
        self.executed_tools = set()

    def _record(self, name, args, started, success, summary):
        self.trace.append(TraceEvent(name=name, arguments=sanitize(args),
                                     duration_ms=round((perf_counter()-started)*1000, 3),
                                     success=success, summary=sanitize(summary), iteration=self.iterations))

    def _execute(self, name, args):
        started = perf_counter()
        self.tool_calls += 1
        try:
            if self.tool_calls > self.MAX_TOOL_CALLS:
                raise ValueError("Tool execution limit reached")
            if name not in TOOL_MODELS:
                raise ValueError("Unknown tool")
            parsed = TOOL_MODELS[name].model_validate(args)
            args = parsed.model_dump(mode="json")
            if name == "search_places":
                # Model cannot change user city to find more convenient fixtures.
                args["city"] = self.preferences.city
                result = search_places(**args, google=self.google, mock=self.mock)
                for data in result["places"]:
                    place = Place.model_validate(data)
                    self.catalog[place.place_id] = place
                success = result["status"] == "ok"
                summary = f"{len(result['places'])} places from {result['source']} ({result['status']})"
            elif name == "get_route":
                args["travel_mode"] = self.preferences.travel_mode
                result = get_route(**args, catalog=self.catalog, google=self.google, mock=self.mock)
                route = Route.model_validate(result)
                self.routes[(route.origin_id, route.destination_id, route.travel_mode)] = route
                success = route.status != "unavailable"
                summary = f"Route {route.status} ({route.source}); {route.duration_minutes} minutes"
            elif name == "estimate_cost":
                result = estimate_cost(**args, budget=self.preferences.budget)
                success = not result["unknown_cost_items"]
                summary = f"INR upper estimate {result['total_estimated_cost']}; {len(result['unknown_cost_items'])} unknown costs"
            else:
                args["preferences"] = self.preferences.model_dump(mode="json")
                result = validate_plan(**args, catalog=self.catalog, routes=self.routes)
                success = result["passed"]
                summary = f"Validation {result['status']}; {len(result['errors'])} errors, {len(result['warnings'])} warnings"
            self.executed_tools.add(name)
        except (ValueError, TypeError, KeyError) as exc:
            # Do not echo validation exception input (may contain user-supplied secrets).
            result = {"error": "Invalid tool arguments or unsupported evidence", "type": type(exc).__name__}
            success, summary = False, result["error"]
        self._record(name, args, started, success, summary)
        return result

    def _search(self, query, area=None):
        result = self._execute("search_places", {"query": query, "city": self.preferences.city, "area": area, "limit": 8})
        return self._retry_empty(result, area)

    def _retry_empty(self, result, area=None):
        if result.get("status") == "empty" and not self.empty_retry_used:
            self.empty_retry_used = True
            # Broadens discovery only. Final validator retains all hard constraints.
            started = perf_counter()
            self._record("relax_soft_preferences", {"retained": self.preferences.hard_constraints}, started, True,
                         "No matches: broaden venue discovery once; retain hard constraints")
            result = self._execute("search_places", {"query": "park restaurant books", "city": self.preferences.city,
                                                     "area": area, "limit": 8})
        return result

    def _validation(self, plan):
        return ValidationResult.model_validate(self._execute("validate_plan", {
            "itinerary": plan.model_dump(mode="json"), "preferences": self.preferences.model_dump(mode="json")}))

    def _result(self, status, message, mode, plan=None, validation=None):
        hydrated = None
        if plan is not None:
            hydrated = plan.model_dump(mode="json")
            for stop in hydrated["stops"]:
                place = self.catalog.get(stop["place_id"])
                stop["venue"] = place.model_dump(mode="json") if place else None
                stop["duration_minutes"] = clock_minutes(stop["end_time"]) - clock_minutes(stop["start_time"])
            edges = []
            previous = plan.starting_place_id
            for stop in plan.stops:
                if previous and previous != stop.place_id:
                    route = self.routes.get((previous, stop.place_id, self.preferences.travel_mode))
                    if route:
                        edges.append(route.model_dump(mode="json"))
                previous = stop.place_id
            hydrated["routes"] = edges
            if validation:
                hydrated["warnings"] = list(dict.fromkeys(hydrated["warnings"] + validation.warnings))
        return PlannerResult(status=status, mode=mode, message=message,
                             preferences=self.preferences.context() if hasattr(self, "preferences") else None,
                             itinerary=hydrated, validation=validation, trace=self.trace,
                             iterations=self.iterations, revisions=self.revisions)

    def plan(self, input_data, *, offline=False):
        self._reset()
        if hasattr(self, "preferences"):
            del self.preferences
        started = perf_counter()
        try:
            self.preferences = Preferences.model_validate(input_data)
        except (ValidationError, ValueError) as exc:
            fields = sorted({".".join(map(str, e["loc"])) or "input" for e in exc.errors()}) if isinstance(exc, ValidationError) else ["input"]
            self._record("parse_preferences", {}, started, False, "Invalid input fields: " + ", ".join(fields))
            return self._result("failure", "Invalid input. Check " + ", ".join(fields) + "; use the sample JSON and valid Saturday/time values.", "offline_demo" if offline else "agent")
        self._record("parse_preferences", self.preferences.context(), started, True,
                     f"Normalized {self.preferences.city}; {self.preferences.duration_minutes} minutes; INR {self.preferences.budget}")
        if offline:
            return self._offline()
        if self.client is None:
            if not os.getenv("OPENAI_API_KEY"):
                return self._result("failure", "OPENAI_API_KEY is missing. Use --mock for the labeled offline demo, or configure a key for the LLM agent.", "agent")
            from openai import OpenAI
            self.client = OpenAI(timeout=30, max_retries=0)
        if self.google is None and self.places_mode != "mock" and os.getenv("GOOGLE_MAPS_API_KEY"):
            self.google = GoogleProvider(os.environ["GOOGLE_MAPS_API_KEY"])
        return self._agent()

    def _agent(self):
        history = [{"role": "system", "content": SYSTEM_PROMPT},
                   {"role": "user", "content": json.dumps(self.preferences.context())}]
        last_plan = last_validation = None
        for iteration in range(1, self.MAX_ITERATIONS + 1):
            self.iterations = iteration
            started = perf_counter()
            try:
                response = self.client.responses.parse(model=self.model, input=history, tools=tool_definitions(),
                                                       text_format=Decision, parallel_tool_calls=False,
                                                       max_output_tokens=4500, store=False)
            except Exception as exc:
                self._record("model_response", {}, started, False,
                             f"OpenAI call failed ({type(exc).__name__}); raw error omitted")
                return self._result("failure", "Model unavailable or returned unusable output. Check configuration and retry; --mock runs offline.",
                                    "agent", last_plan, last_validation)
            # Keep ALL output items, including opaque reasoning items required by Responses.
            # They are sent back to the API only and are never included in our public trace.
            history.extend(response.output)
            calls = [item for item in response.output if item.type == "function_call"]
            self._record("model_response", {}, started, True, f"Model requested {len(calls)} tool actions" if calls else "Model returned final decision")
            if calls:
                for call in calls:
                    try:
                        args = json.loads(call.arguments)
                    except (ValueError, TypeError):
                        args = {}
                    result = self._execute(call.name, args)
                    if call.name == "search_places":
                        result = self._retry_empty(result, args.get("area") if isinstance(args, dict) else None)
                    history.append({"type": "function_call_output", "call_id": call.call_id,
                                    "output": json.dumps(result, allow_nan=False)})
                if self.tool_calls >= self.MAX_TOOL_CALLS:
                    break
                continue
            decision = response.output_parsed
            if decision is None:
                return self._result("failure", "Model refused or did not provide a structured decision. Try a simpler request.", "agent")
            if decision.status == "infeasible":
                if decision.itinerary is not None:
                    return self._result("failure", "Model returned an inconsistent infeasibility response.", "agent")
                return self._result("infeasible", decision.message, "agent")
            if decision.itinerary is None:
                history.append({"role": "user", "content": "A recommendation must contain an itinerary; supply it or return infeasible."})
                continue
            last_plan = decision.itinerary
            last_validation = self._validation(last_plan)
            if not last_validation.errors:
                if len(self.executed_tools) < 3:
                    history.append({"role": "user", "content": "Before finalizing, execute at least three distinct tools, including estimate_cost. Venue and route evidence must be obtained through tools."})
                    continue
                return self._result("success" if last_validation.passed else "conditional", decision.message,
                                    "agent", last_plan, last_validation)
            if self.revisions >= self.MAX_REVISIONS:
                return self._result("infeasible", "Could not satisfy hard feasibility checks after two revisions; the attached draft is invalid and must not be followed.",
                                    "agent", last_plan, last_validation)
            self.revisions += 1
            started = perf_counter()
            self._record("validation_revision", {"attempt": self.revisions, "errors": last_validation.errors},
                         started, True, "Requested itinerary revision after independent validation failure")
            history.append({"role": "user", "content": "Independent server validation failed. Revise or return infeasible: " + last_validation.model_dump_json()})
        return self._result("failure", "Agent reached its six-iteration or tool-call limit. Any attached draft is unapproved; try a simpler request.",
                            "agent", last_plan, last_validation)

    def _offline(self):
        # No LLM impersonation: this is a small reproducible demonstration of the tools.
        self.google = None
        prefs = self.preferences
        result = self._search(" ".join(prefs.interests))
        if not result.get("places"):
            return self._result("infeasible", "No matching mock venues after one broader search. Offline fixtures cover Bengaluru only; configure live APIs for other cities.", "offline_demo")
        origin_id = None
        if prefs.starting_neighborhood:
            origin_results = self._search(prefs.starting_neighborhood)
            matches = [p for p in origin_results.get("places", [])
                       if prefs.starting_neighborhood.lower() in (p["name"] + " " + p["address"]).lower()]
            if not matches:
                return self._result("infeasible", "Offline fixtures cannot resolve the starting neighborhood; choose central Bengaluru or use live mode.", "offline_demo")
            origin_id = matches[0]["place_id"]
        candidates = list(self.catalog.values())
        center = self.catalog.get(origin_id) or next((p for p in candidates if p.place_id == "mock:cubbon"), candidates[0])
        candidates.sort(key=lambda p: (p.latitude-center.latitude)**2 + (p.longitude-center.longitude)**2)
        selected = []
        for place in candidates:
            is_food = "food" in place.types
            if is_food and any(place.evidence.get(c) is not True for c in prefs.hard_constraints if c in {"vegetarian", "vegan", "pure vegetarian"}):
                continue
            if not selected or (place.latitude-center.latitude)**2 + (place.longitude-center.longitude)**2 < .0002:
                selected.append(place)
            if len(selected) == 3:
                break
        stops = []
        cursor = clock_minutes(prefs.start_time)
        previous = origin_id
        low_energy = any(word in prefs.mood.lower() for word in ("tired", "exhausted", "low energy", "relax"))
        for place in selected:
            if previous and previous != place.place_id:
                route = self._execute("get_route", {"origin": {"place_id": previous, "latitude": None, "longitude": None},
                    "destination": {"place_id": place.place_id, "latitude": None, "longitude": None}, "travel_mode": prefs.travel_mode})
                if route.get("duration_minutes") is None:
                    return self._result("infeasible", "Travel could not be estimated for the offline route.", "offline_demo")
                cursor += math.ceil(route["duration_minutes"])
            cursor += 10
            kind = "food" if "food" in place.types else ("walk" if "park" in place.types else "culture")
            duration = 40 if kind == "food" else (25 if low_energy else 40)
            if cursor + duration >= 1440:
                return self._result("infeasible", "The offline activity would cross midnight. Use an earlier start or live planning.", "offline_demo")
            rationale = ("A short, gentle stroll with time to sit suits your low-energy mood." if kind == "walk" and low_energy else
                         "A nearby food break fits your food interest; vegetarian options are mock fixture information." if kind == "food" else
                         "A nearby unhurried stop adds variety without a long journey.")
            activity = "Gentle stroll and seated rest" if kind == "walk" else ("Meal and rest" if kind == "food" else "Browse at your own pace")
            if kind == "walk" and "music" in prefs.interests:
                activity += "; optional music on headphones while seated"
                rationale += " Headphones cover your music interest without assuming a live event."
            stops.append(Stop(place_id=place.place_id, activity=activity, kind=kind,
                              start_time=clock_string(cursor), end_time=clock_string(cursor+duration),
                              buffer_minutes=10, cost=place.estimated_price, rationale=rationale))
            cursor += duration
            previous = place.place_id
        transport = Price(minimum=0, maximum=0, confidence="verified", basis="Walking has no fare") if prefs.travel_mode == "walking" else Price(
            minimum=150, maximum=500, confidence="estimated", basis="Illustrative local driving fuel/fare/parking allowance")
        plan = Itinerary(title="An easy Saturday in Bengaluru", stops=stops, starting_place_id=origin_id,
                         transport_cost=transport, warnings=["Offline demonstration: no LLM or live API calls; all venue fixtures need confirmation."],
                         trade_offs=["No live music or quiet crowd levels are assumed.", "This plan leaves spare time for rest."])
        while True:
            self._execute("estimate_cost", {"activities": [{"label": s.activity, "cost": s.cost.model_dump()} for s in plan.stops],
                                           "transport_cost": plan.transport_cost.model_dump()})
            validation = self._validation(plan)
            if not validation.errors:
                return self._result("conditional", "Provisional mock plan. Check current hours, prices, dietary options and walking access before following it.",
                                    "offline_demo", plan, validation)
            if self.revisions >= self.MAX_REVISIONS or len(plan.stops) == 1:
                return self._result("infeasible", "No feasible offline itinerary within the supplied time/budget or hard constraints. Invalid draft attached for transparency.",
                                    "offline_demo", plan, validation)
            self.revisions += 1
            started = perf_counter()
            self._record("validation_revision", {"attempt": self.revisions, "errors": validation.errors}, started, True,
                         "Offline fallback removed the final stop to reduce time/cost")
            plan.stops.pop()
