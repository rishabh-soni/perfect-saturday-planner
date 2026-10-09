"""Bounded provider-neutral agent plus an explicit deterministic offline demo."""
from __future__ import annotations

import math
import os
import re
from time import perf_counter

from pydantic import ValidationError

from agent.prompts import SYSTEM_PROMPT
from agent.schemas import (Decision, Itinerary, Place, PlanOption, PlannerResult, Preferences,
                           Price, Route, Stop, TraceEvent, ValidationResult, clock_minutes, clock_string)
from providers.google import GoogleProvider, ProviderError
from providers.cache import ProviderCache
from providers.nominatim import NominatimProvider
from providers.osm import OverpassProvider, distance_meters
from providers.weather import OpenMeteoProvider
from providers.llm import ModelError, ToolResult
from providers.mock import MockProvider
from tools.budget import BudgetArguments, estimate_cost
from tools.weather import WeatherArguments
from tools.places import SearchArguments, search_places
from tools.routing import RouteArguments, get_route
from tools.validator import ValidationArguments, opening_check, validate_plan


TOOL_MODELS = {"search_places": SearchArguments, "get_route": RouteArguments,
               "estimate_cost": BudgetArguments, "validate_plan": ValidationArguments, "get_weather": WeatherArguments}
DESCRIPTIONS = {
    "search_places": "Discover actual OSM venues around the submitted city/neighborhood anchor. Search tag categories, reuse the cached compact area. Missing metadata is unknown; service failure may use labeled Bengaluru fixtures.",
    "get_weather": "Read retrieved Open-Meteo hourly weather for the original Saturday and window. It is already in context; repeat reads reuse it. Prefer indoor activities in adverse conditions.",
    "get_route": "Get walking/driving route between discovered place IDs or their exact coordinates. Failure is unavailable, mock routes are estimates.",
    "estimate_cost": "Calculate INR itemized upper/lower estimated costs against the user's original budget. Unknown costs remain unknown.",
    "validate_plan": "Check chronology, route evidence, time, original budget, hard constraints, Saturday hours and critical unknowns. Original preferences are enforced.",
}


def sanitize(value):
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if re.search(r"key|secret|token|authorization|password", str(k), re.I)
                else sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value[:30]]
    if isinstance(value, str):
        value = re.sub(r"(?:sk-|gsk_)[A-Za-z0-9_-]+", "[REDACTED]", value)
        value = re.sub(r"AIza[A-Za-z0-9_-]+", "[REDACTED]", value)
        for key in (os.getenv("GROQ_API_KEY"), os.getenv("GEMINI_API_KEY"), os.getenv("OPENAI_API_KEY"), os.getenv("GOOGLE_MAPS_API_KEY")):
            if key:
                value = value.replace(key, "[REDACTED]")
        return value[:3000]
    return value


class Planner:
    MAX_ITERATIONS = 12
    MAX_REVISIONS = 2
    MAX_TOOL_CALLS = 24

    def __init__(self, *, client=None, llm=None, google=None, mock=None, model=None, places_mode="auto", on_event=None, osm=None, geocoder=None, weather=None, cache=None):
        try:
            configured_limit = int(os.getenv("AGENT_MAX_ITERATIONS", str(self.MAX_ITERATIONS)))
        except ValueError:
            configured_limit = self.MAX_ITERATIONS
        self.max_iterations = min(20, max(3, configured_limit))
        self.client = client
        self.llm = llm
        self.google = google
        self.mock = mock or MockProvider()
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        self.places_mode = places_mode
        self.on_event = on_event
        self.osm, self.geocoder, self.weather_provider, self.provider_cache = osm, geocoder, weather, cache

    def _reset(self):
        self.catalog = {}
        self.routes = {}
        self.trace = []
        self.iterations = self.revisions = self.tool_calls = 0
        self.empty_retry_used = False
        self.infeasible_retry_used = False
        self.last_candidate = None
        self.executed_tools = set()
        self.location = self.weather_data = self.origin_place_id = None
        self.provider_warnings = []

    def _record(self, name, args, started, success, summary):
        event = TraceEvent(name=name, arguments=sanitize(args),
                           duration_ms=round((perf_counter()-started)*1000, 3),
                           success=success, summary=sanitize(summary), iteration=self.iterations)
        self.trace.append(event)
        if self.on_event:
            self.on_event(event.model_copy(deep=True))

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
                result = search_places(**args, osm=self.osm if self.places_mode != "mock" else None,
                                       primary_osm=self.places_mode != "mock", mock=self.mock)
                result["weather"] = self.weather_data
                result["origin"] = self.catalog[self.origin_place_id].model_dump(mode="json") if self.origin_place_id else None
                self.provider_warnings.extend(w for w in result.get("warnings", []) if w not in self.provider_warnings)
                for data in result["places"]:
                    place = Place.model_validate(data)
                    self.catalog[place.place_id] = place
                success = result["status"] == "ok"
                summary = f"{len(result['places'])} places from {result['source']} ({result['status']})"
            elif name == "get_route":
                args["travel_mode"] = self.preferences.travel_mode
                result = get_route(**args, catalog=self.catalog, google=self.google, mock=self.mock, route_cache=self.routes)
                route = Route.model_validate(result)
                self.routes[(route.origin_id, route.destination_id, route.travel_mode)] = route
                success = route.status != "unavailable"
                summary = f"Route {route.status} ({route.source}); {route.duration_minutes} minutes"
            elif name == "estimate_cost":
                result = estimate_cost(**args, budget=self.preferences.budget)
                success = not result["unknown_cost_items"]
                summary = f"INR upper estimate {result['total_estimated_cost']}; {len(result['unknown_cost_items'])} unknown costs"
            elif name == "get_weather":
                result = self.weather_data or {"status": "unavailable", "hourly": [], "guidance": "No forecast in mock/demo mode"}
                success = result["status"] == "available"
                summary = f"Weather {result['status']}; original Saturday/time window retained"
            else:
                self.last_candidate = parsed.itinerary.model_copy(deep=True)
                args["preferences"] = self.preferences.model_dump(mode="json")
                result = validate_plan(**args, catalog=self.catalog, routes=self.routes, weather=self.weather_data)
                success = result["passed"]
                summary = f"Validation {result['status']}; {len(result['errors'])} errors, {len(result['warnings'])} warnings"
            self.executed_tools.add(name)
        except (ValueError, TypeError, KeyError) as exc:
            # Do not echo validation exception input (may contain user-supplied secrets).
            result = {"error": "Invalid tool arguments or unsupported evidence", "type": type(exc).__name__}
            if name == "get_route":
                result["guidance"] = "Use origin/destination place_id copied exactly from search_places results, with latitude=null and longitude=null. Do not route from an unspecified home."
            if isinstance(exc, ValidationError):
                result["argument_errors"] = sanitize([
                    {"field": ".".join(map(str, e["loc"])), "type": e["type"], "message": e["msg"]}
                    for e in exc.errors(include_input=False, include_context=False, include_url=False)])
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

    def _recover_simpler(self, candidate):
        """Bounded recovery of an LLM draft; all evidence/constraints remain authoritative."""
        if candidate is None or self.revisions >= self.MAX_REVISIONS or self.tool_calls > self.MAX_TOOL_CALLS - 2:
            return None
        # Prefer a discovered food stop; never synthesize a venue, route or price.
        options = sorted(candidate.stops, key=lambda stop: stop.kind != "food")
        for stop in options:
            if stop.place_id not in self.catalog:
                continue
            duration = clock_minutes(stop.end_time) - clock_minutes(stop.start_time)
            if duration <= 0:
                continue
            origin = candidate.starting_place_id if self.preferences.starting_neighborhood else None
            travel = 0
            if origin and origin != stop.place_id:
                route = self.routes.get((origin, stop.place_id, self.preferences.travel_mode))
                if route is None or route.status == "unavailable" or route.duration_minutes is None:
                    continue
                travel = route.duration_minutes
            begin = max(clock_minutes(stop.start_time), math.ceil(clock_minutes(self.preferences.start_time) + travel + stop.buffer_minutes))
            end = begin + duration
            if end >= 1440 or end > clock_minutes(self.preferences.start_time) + self.preferences.duration_minutes:
                continue
            shortened = candidate.model_copy(deep=True)
            selected = stop.model_copy(update={"start_time": clock_string(begin), "end_time": clock_string(end)}, deep=True)
            shortened.stops = [selected]
            shortened.starting_place_id = origin
            shortened.title = f"A simpler Saturday in {self.preferences.city}"
            shortened.trade_offs = [
                "This shorter plan prioritizes one activity; separate stops for every interest are omitted to reduce travel and uncertainty."]
            if "music" in self.preferences.interests:
                shortened.trade_offs.append("Music can be an optional seated headphone break; no live event or venue music is verified.")
            if any(interest in self.preferences.interests for interest in ("walks", "walk")) and selected.kind != "walk":
                shortened.trade_offs.append("A separate walk is omitted from this shorter itinerary.")
            shortened.warnings = [
                "A shorter fallback from discovered venue evidence; crowd levels and venue music remain unverified."]
            self.revisions += 1
            self._record("simplify_plan", {"place_id": selected.place_id, "attempt": self.revisions,
                "start_time": selected.start_time, "end_time": selected.end_time}, perf_counter(), True,
                "Reduced model draft to one discovered stop; retained hard constraints and original time window")
            self._execute("estimate_cost", {"activities": [{"label": selected.activity, "cost": selected.cost.model_dump()}],
                "transport_cost": shortened.transport_cost.model_dump()})
            validation = self._validation(shortened)
            if not validation.errors and len(self.executed_tools) >= 3:
                return self._result("success" if validation.passed else "conditional",
                    "A shorter plan is available using a discovered venue. Separate activities for all interests are omitted; review the estimates and confirmation warnings.",
                    "agent", shortened, validation)
            # One recovery validation per attempt; never relax a failed hard check.
            return None
        return None

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
                             iterations=self.iterations, revisions=self.revisions, weather=self.weather_data,
                             location=self.location, provider_warnings=self.provider_warnings)

    def add_alternatives(self, result, *, limit=3):
        """Compare bounded, evidence-backed variants after the genuine agent run.

        This is a traced business-rule fallback, not an additional LLM response.
        Every offered option retains the original hard constraints and is validated.
        """
        if not self.catalog or not hasattr(self, "preferences"):
            return result
        if result.mode == "agent" and len(self.executed_tools) < 3:
            return result
        base = self.last_candidate
        if result.itinerary:
            raw = {k: result.itinerary[k] for k in Itinerary.model_fields}
            raw["stops"] = [{k: stop[k] for k in Stop.model_fields} for stop in raw["stops"]]
            base = Itinerary.model_validate(raw)
        if base is None:
            return result
        options = []
        signatures = set()
        if result.status in {"success", "conditional"} and result.validation and not result.validation.errors:
            options.append(PlanOption(id="option-1", label="Recommended day", message=result.message,
                                      status=result.status, itinerary=result.itinerary, validation=result.validation))
            signatures.add(tuple(s.place_id for s in base.stops))
        self._record("compare_options", {"requested_options": limit}, perf_counter(), True,
                     "Comparing shorter alternatives from discovered places; retaining original constraints")
        # Prefer the model's own stops and prices before other discovered venues.
        candidates = list(base.stops)
        seen = {s.place_id for s in candidates}
        interests = set(self.preferences.interests)
        for place in self.catalog.values():
            if place.place_id in seen:
                continue
            types = set(place.types)
            if types & {"restaurant", "cafe", "food"} and "food" in interests:
                kind, activity, minutes = "food", "An unhurried food break", 45
            elif types & {"park", "walks", "nature"} and interests & {"walk", "walks", "nature"}:
                kind, activity, minutes = "walk", "A gentle walk with time to sit", 35
            elif types & {"museum", "art", "culture", "book_store", "books"} and interests & {"art", "culture", "books"}:
                kind, activity, minutes = "culture", "Browse at your own pace", 40
            else:
                continue
            candidates.append(Stop(place_id=place.place_id, kind=kind, activity=activity,
                start_time="00:00", end_time=clock_string(minutes), buffer_minutes=10,
                cost=place.estimated_price or Price(minimum=None, maximum=None, confidence="unknown",
                    basis="No current menu or admission price was supplied by the place provider"),
                rationale="Prioritizes one of your interests with fewer stops and less travel."))
        if self.weather_data and self.weather_data.get("prefer_indoor"):
            candidates.sort(key=lambda stop: self.catalog[stop.place_id].environment != "indoor")
        for stop in candidates:
            if len(options) >= min(3, max(1, limit)) or self.tool_calls > self.MAX_TOOL_CALLS - 3:
                break
            if (stop.place_id,) in signatures or stop.place_id not in self.catalog:
                continue
            origin = base.starting_place_id if self.preferences.starting_neighborhood else None
            if self.preferences.starting_neighborhood and not origin:
                continue
            travel = 0
            if origin and origin != stop.place_id:
                edge = (origin, stop.place_id, self.preferences.travel_mode)
                if edge not in self.routes:
                    self._execute("get_route", {"origin": {"place_id": origin, "latitude": None, "longitude": None},
                        "destination": {"place_id": stop.place_id, "latitude": None, "longitude": None},
                        "travel_mode": self.preferences.travel_mode})
                route = self.routes.get(edge)
                if route is None or route.status == "unavailable" or route.duration_minutes is None:
                    continue
                travel = route.duration_minutes
            length = min(60, clock_minutes(stop.end_time) - clock_minutes(stop.start_time))
            if length <= 0:
                continue
            earliest = math.ceil(clock_minutes(self.preferences.start_time) + travel + stop.buffer_minutes)
            deadline = clock_minutes(self.preferences.start_time) + self.preferences.duration_minutes
            # A later opening can fit the same window; do not declare it impossible at the first slot.
            begin = next((t for t in range(earliest, deadline - length + 1, 15)
                          if opening_check(self.catalog[stop.place_id], t, t + length) is not False), None)
            if begin is None or begin + length >= 1440:
                continue
            selected = stop.model_copy(update={"start_time": clock_string(begin), "end_time": clock_string(begin + length)}, deep=True)
            place = self.catalog[stop.place_id]
            omitted = sorted(interests - ({"food"} if stop.kind == "food" else {"walk", "walks", "nature"} if stop.kind == "walk" else {"art", "culture", "books"}))
            explanation = f"Focus on {place.name} for {length} minutes, with fewer stops and more spare time."
            trade_offs = ["This alternative reduces travel by keeping one venue."]
            if omitted:
                trade_offs.append("Separate activities for " + ", ".join(omitted) + " are omitted; they remain optional preferences.")
            candidate = Itinerary(title=f"A slower Saturday: {place.name}", stops=[selected],
                starting_place_id=origin, transport_cost=base.transport_cost,
                warnings=["Venue hours, prices and crowd levels require the confirmations listed below."], trade_offs=trade_offs)
            if self.weather_data and self.weather_data.get("prefer_indoor") and place.environment == "indoor":
                candidate.trade_offs.append("An indoor venue is preferred because the retrieved forecast indicates adverse outdoor conditions.")
            self._execute("estimate_cost", {"activities": [{"label": selected.activity, "cost": selected.cost.model_dump()}],
                                           "transport_cost": candidate.transport_cost.model_dump()})
            validation = self._validation(candidate)
            if validation.errors:
                continue
            status = "success" if validation.passed else "conditional"
            hydrated = self._result(status, explanation, result.mode, candidate, validation)
            options.append(PlanOption(id=f"option-{len(options)+1}", label=f"{place.name} · shorter break",
                message=explanation, status=status, itinerary=hydrated.itinerary, validation=validation))
            signatures.add((stop.place_id,))
        if options and result.status not in {"success", "conditional"}:
            first = options[0]
            result = self._result(first.status, "The full request did not fit the checked evidence. " + first.message,
                                  result.mode)
            result.itinerary, result.validation = first.itinerary, first.validation
            result.status = first.status
        result.alternatives = options
        result.trace = list(self.trace)
        return result

    def _prepare_context(self):
        """Resolve only user-submitted locations, then fetch one cached forecast."""
        if self.places_mode == "mock":
            return True
        cache = self.provider_cache or ProviderCache()
        self.geocoder = self.geocoder or NominatimProvider(cache=cache)
        self.weather_provider = self.weather_provider or OpenMeteoProvider(cache=cache)
        started = perf_counter()
        try:
            self.location = self.geocoder.resolve(self.preferences.city)
            self._record("resolve_city", {"city": self.preferences.city}, started, True,
                         f"Nominatim resolved city; cached={self.location.get('cached', False)}")
        except ProviderError as exc:
            self.provider_warnings.append(str(exc))
            self._record("resolve_city", {"city": self.preferences.city}, started, False, str(exc))
            self.osm = None
            self.weather_data = {"status": "unavailable", "hourly": [], "prefer_indoor": False,
                "guidance": "Location unresolved; weather cannot be retrieved. Keep an indoor backup.", "warnings": [str(exc)]}
            return not self.preferences.starting_neighborhood
        anchor = self.location
        if self.preferences.starting_neighborhood:
            started = perf_counter()
            try:
                anchor = self.geocoder.resolve(self.preferences.city, self.preferences.starting_neighborhood)
                if distance_meters((anchor["latitude"], anchor["longitude"]),
                                   (self.location["latitude"], self.location["longitude"])) > 50000:
                    raise ProviderError("Starting neighborhood resolved too far from the requested city; clarify the location")
                origin = Place(place_id=f"origin:{anchor['osm_type']}:{anchor['osm_id']}",
                    name=anchor["display_name"], address=anchor["display_name"], latitude=anchor["latitude"],
                    longitude=anchor["longitude"], source="nominatim", types=["origin"],
                    resolved_neighborhood=self.preferences.starting_neighborhood,
                    notes=["Geocoded neighborhood reference point, not a venue or precise home address. © OpenStreetMap contributors · ODbL"])
                self.catalog[origin.place_id] = origin
                self.origin_place_id = origin.place_id
                self._record("resolve_starting_neighborhood", {"city": self.preferences.city, "neighborhood": self.preferences.starting_neighborhood},
                    started, True, f"Resolved starting reference point {origin.place_id}; cached={anchor.get('cached', False)}")
            except ProviderError as exc:
                self._record("resolve_starting_neighborhood", {"neighborhood": self.preferences.starting_neighborhood}, started, False, str(exc))
                self.provider_warnings.append(str(exc))
                return False
        self.osm = self.osm or OverpassProvider(anchor=anchor, cache=cache)
        self.osm.anchor = anchor
        started = perf_counter()
        self.weather_data = self.weather_provider.forecast(anchor, self.preferences)
        self.osm.prefer_indoor = self.weather_data.get("prefer_indoor", False)
        self._record("get_weather", {"saturday": str(self.preferences.saturday), "start_time": self.preferences.start_time,
            "duration_minutes": self.preferences.duration_minutes}, started, self.weather_data["status"] == "available",
            f"Open-Meteo {self.weather_data['status']}; {self.weather_data.get('guidance', '')}; cached={self.weather_data.get('cached', False)}")
        return True

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
            original_mode = self.places_mode
            try:
                return self._offline()
            finally:
                self.places_mode = original_mode
        owned = self.llm is None
        if owned:
            if self.client is None and not os.getenv("OPENAI_API_KEY", "").strip():
                return self._result("failure", "OPENAI_API_KEY is missing. Use --mock for the labeled offline demo, or configure a key for the LLM agent.", "agent")
            from providers.openai import OpenAIModel
            try:
                self.llm = OpenAIModel(api_key=os.getenv("OPENAI_API_KEY"), model=self.model, client=self.client)
            except Exception:
                return self._result("failure", "Could not initialize OpenAI. Check server configuration or use the demo.", "agent")
        try:
            if self.google is None and self.places_mode != "mock" and os.getenv("GOOGLE_MAPS_API_KEY"):
                self.google = GoogleProvider(os.environ["GOOGLE_MAPS_API_KEY"])
            if not self._prepare_context():
                return self._result("failure", "The starting neighborhood could not be resolved reliably. Clarify it or omit it; no origin or journey was invented.", "agent")
            return self._agent()
        finally:
            if owned:
                self.llm.close()
                self.llm = None

    def _agent(self):
        last_plan = last_validation = None
        try:
            session = self.llm.start(SYSTEM_PROMPT + f"\nYou have {self.max_iterations} model turns total. Reserve turns for validation, correction and submit_plan.", {**self.preferences.context(), "location": self.location, "weather": self.weather_data,
                    "starting_origin": self.catalog[self.origin_place_id].model_dump(mode="json") if self.origin_place_id else None}, TOOL_MODELS, DESCRIPTIONS)
        except Exception:
            return self._result("failure", "Could not initialize model session. Check configuration or use the demo.", "agent")
        for iteration in range(1, self.max_iterations + 1):
            self.iterations = iteration
            started = perf_counter()
            try:
                if iteration == self.max_iterations - 2:
                    message = "Three model turns remain, including this one. Reuse discovered nearby venues and recorded routes; avoid new discovery unless essential. Shorten an infeasible plan, validate necessary changes, then call submit_plan alone. Prefer a shorter conditional plan over abandoning soft interests."
                    session.feedback(message)
                    self._record("planning_budget", {"remaining_turns": 3}, perf_counter(), True,
                                 "Asked model to finish within remaining turn budget")
                if iteration == self.max_iterations - 1 and callable(getattr(session, "finalize", None)):
                    session.finalize()
                    self._record("planning_budget", {"remaining_turns": 2, "phase": "final_submission"}, perf_counter(), True,
                                 "Reserved final two turns for structured submission and correction")
                turn = session.next_turn()
                if turn.correction:
                    self._record("model_response", turn.diagnostics, started, False, "Invalid structured submission; requested correction")
                    session.feedback(turn.correction)
                    continue
                self._record("model_response", turn.diagnostics, started, True,
                             f"Model requested {len(turn.calls)} tool actions" if turn.calls else "Model returned final decision")
                if turn.calls:
                    outputs = []
                    for call in turn.calls:
                        result = self._execute(call.name, call.arguments)
                        if call.name == "search_places":
                            result = self._retry_empty(result, call.arguments.get("area"))
                        outputs.append(ToolResult(request=call, output=result))
                    session.respond(outputs)
                    if self.tool_calls >= self.MAX_TOOL_CALLS:
                        break
                    continue
                decision = turn.decision
                if decision is None:
                    return self._result("failure", "Model did not provide a structured decision. Try a simpler request.", "agent")
                if decision.status == "infeasible":
                    if decision.itinerary is not None:
                        return self._result("failure", "Model returned an inconsistent infeasibility response.", "agent")
                    recovered = self._recover_simpler(last_plan or self.last_candidate)
                    if recovered is not None:
                        return recovered
                    if not self.infeasible_retry_used and iteration < self.max_iterations and self.tool_calls < self.MAX_TOOL_CALLS:
                        self.infeasible_retry_used = True
                        self._record("infeasibility_review", {"discovered_venues": len(self.catalog)}, perf_counter(), True,
                                     "Challenged unsupported infeasibility; requested a shorter best-effort plan")
                        session.feedback("Your infeasibility claim has not been established. Attempt a simpler one- or two-stop recommendation using discovered nearby venues before giving up. Interests and avoiding crowds are soft preferences, not mandatory separate activities. Estimated costs are allowed; unknown hours/prices/dietary evidence require warnings, not automatic rejection. Keep the original hard constraints, budget and travel mode. The supplied start time begins the available window: add the first buffer, e.g. 15:00 arrival plus 10 minutes means 15:10 activity, which is allowed. Include a nonzero estimated driving allowance. Reuse recorded routes or omit unnecessary transitions; no supplied neighborhood means starting_place_id=null. Explain omitted interests and any confirmation warnings. If evidence really prevents even this smaller plan, return infeasible with the specific candidate-level blockers; do not claim the entire city or request is impossible.")
                        continue
                    return self._result("failure", "The agent could not find a validated plan within its current search and planning limits. This does not establish that your preferences are impossible. Retry with a starting neighborhood to focus discovery.",
                                        "agent", last_plan, last_validation)
                if decision.itinerary is None:
                    session.feedback("A recommendation must contain an itinerary; supply it or return infeasible.")
                    continue
                last_plan = decision.itinerary
                last_validation = self._validation(last_plan)
                if not last_validation.errors:
                    if len(self.executed_tools) < 3:
                        session.feedback("Before finalizing, execute at least three distinct tools, including estimate_cost. Venue and route evidence must be obtained through tools.")
                        continue
                    return self._result("success" if last_validation.passed else "conditional", decision.message,
                                        "agent", last_plan, last_validation)
                if iteration >= self.max_iterations - 1 or self.revisions == self.MAX_REVISIONS - 1:
                    recovered = self._recover_simpler(last_plan)
                    if recovered is not None:
                        return recovered
                if self.revisions >= self.MAX_REVISIONS:
                    return self._result("failure", "The agent could not validate its proposed plan after two revisions. This does not establish that your request is impossible; any attached draft is unapproved.",
                                        "agent", last_plan, last_validation)
                self.revisions += 1
                self._record("validation_revision", {"attempt": self.revisions, "errors": last_validation.errors},
                             perf_counter(), True, "Requested itinerary revision after independent validation failure")
                session.feedback("Independent server validation failed. Revise or return infeasible: " + last_validation.model_dump_json())
            except Exception as exc:
                diagnostics = getattr(exc, "diagnostics", {})
                detail = f"HTTP {diagnostics['http_status']} {diagnostics.get('api_status') or ''}" if diagnostics.get("http_status") else type(exc).__name__
                self._record("model_response", diagnostics, started, False,
                             f"Model call failed ({detail}); inspect sanitized diagnostics for details")
                message = str(exc) if isinstance(exc, ModelError) else "Model unavailable or returned unusable output. Check configuration and retry; the Bengaluru demo remains available."
                return self._result("failure", sanitize(message), "agent", last_plan, last_validation)
        recovered = self._recover_simpler(last_plan or self.last_candidate)
        if recovered is not None:
            return recovered
        limit = f"{self.MAX_TOOL_CALLS}-tool-call" if self.tool_calls >= self.MAX_TOOL_CALLS else f"{self.max_iterations}-iteration"
        return self._result("failure", f"Agent reached its {limit} limit after {self.iterations} model turns and {self.tool_calls} tool calls. Any attached draft is unapproved; try fewer stops or a smaller area.",
                            "agent", last_plan, last_validation)

    def _offline(self):
        # No LLM impersonation: this is a small reproducible demonstration of the tools.
        self.google = self.osm = None
        self.places_mode = "mock"
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
