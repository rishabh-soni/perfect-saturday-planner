"""Independent validation against provider evidence, never model assertions."""
from agent.schemas import Itinerary, Place, Preferences, Route, Schema, ValidationResult, clock_minutes
from tools.budget import estimate_cost


class ValidationArguments(Schema):
    itinerary: Itinerary
    preferences: Preferences


def opening_check(place: Place, start: int, end: int) -> bool | None:
    """Google weekly periods use Sunday=0. Include Friday overnight periods."""
    hours = place.opening_hours
    if hours is None:
        return None
    periods = hours.get("periods")
    if periods is None:
        return None
    if not periods:
        return False
    try:
        for period in periods:
            opened, closed = period["open"], period.get("close")
            if closed is None:
                if opened.get("day") == 0 and opened.get("hour", 0) == 0 and opened.get("minute", 0) == 0:
                    return True  # Google's documented 24/7 marker.
                return None
            begin = opened["day"] * 1440 + opened.get("hour", 0) * 60 + opened.get("minute", 0)
            finish = closed["day"] * 1440 + closed.get("hour", 0) * 60 + closed.get("minute", 0)
            if finish <= begin:
                finish += 7 * 1440
            for offset in (0, 7 * 1440):
                if begin <= 6 * 1440 + start + offset and 6 * 1440 + end + offset <= finish:
                    return True
        return False
    except (KeyError, TypeError, ValueError):
        return None


def validate_plan(itinerary, preferences, *, catalog=None, routes=None) -> dict:
    plan = Itinerary.model_validate(itinerary)
    prefs = Preferences.model_validate(preferences)
    catalog, routes = catalog or {}, routes or {}
    errors, warnings = [], []
    critical_unknown = False
    start = clock_minutes(prefs.start_time)
    deadline = start + prefs.duration_minutes
    previous_end = start
    previous_id = plan.starting_place_id
    if prefs.starting_neighborhood:
        origin = catalog.get(previous_id)
        if origin is None or prefs.starting_neighborhood.casefold() not in (origin.address + " " + origin.name).casefold():
            errors.append("Starting neighborhood requires a discovered matching origin and route to the first stop")
    elif previous_id is None:
        warnings.append("Window starts at the first venue; travel from home and return travel are excluded")
    ids = [s.place_id for s in plan.stops]
    if len(set(ids)) != len(ids):
        errors.append("Duplicate venue stops are not allowed")
    for stop in plan.stops:
        begin, end = clock_minutes(stop.start_time), clock_minutes(stop.end_time)
        if begin < start or end <= begin:
            errors.append(f"Invalid chronological activity interval: {stop.place_id}")
        route_minutes = 0
        if previous_id is not None and previous_id != stop.place_id:
            route = routes.get((previous_id, stop.place_id, prefs.travel_mode))
            if route is None or route.status == "unavailable" or route.duration_minutes is None:
                errors.append(f"Missing usable route: {previous_id} -> {stop.place_id}")
            else:
                route_minutes = route.duration_minutes
                if route.status == "estimated":
                    critical_unknown = True
                    warnings.append(f"Unverified mock route: {previous_id} -> {stop.place_id}")
        if begin < previous_end + route_minutes + stop.buffer_minutes:
            errors.append(f"Not enough time for travel and buffer before {stop.place_id}")
        if end > deadline:
            errors.append(f"Available time exceeded at {stop.place_id}")
        place = catalog.get(stop.place_id)
        if place is None:
            errors.append(f"Venue not discovered by tools: {stop.place_id}")
        else:
            is_food = stop.kind == "food" or any(t in place.types for t in ("restaurant", "cafe", "food"))
            if place.source == "mock":
                critical_unknown = True
                warnings.append(f"Mock venue information: {place.name}")
            opened = opening_check(place, begin, end)
            if opened is False:
                errors.append(f"Activity outside available Saturday opening hours: {place.name}")
            elif opened is None:
                critical_unknown = True
                warnings.append(f"Opening hours unknown: {place.name}; check before visiting")
            for constraint in prefs.hard_constraints:
                normalized = {"pure vegetarian": "pure_vegetarian", "no alcohol": "alcohol_free",
                              "wheelchair accessible": "wheelchair"}.get(constraint, constraint)
                if normalized in {"vegetarian", "vegan", "pure_vegetarian"} and not is_food:
                    continue
                if normalized not in {"vegetarian", "vegan", "pure_vegetarian", "wheelchair", "alcohol_free"}:
                    critical_unknown = True
                    warnings.append(f"Hard constraint requires manual confirmation: {constraint}")
                elif place.evidence.get(normalized) is False:
                    errors.append(f"Hard constraint violated ({constraint}): {place.name}")
                elif place.evidence.get(normalized) is not True:
                    critical_unknown = True
                    warnings.append(f"Hard constraint unverified ({constraint}): {place.name}")
            if stop.cost.confidence == "verified":
                # Places priceLevel is NOT a verified menu or admission price.
                errors.append(f"Verified activity price unsupported by available providers: {place.name}")
        previous_id, previous_end = stop.place_id, end
    costs = estimate_cost([{"label": s.activity, "cost": s.cost.model_dump()} for s in plan.stops],
                          plan.transport_cost.model_dump(), budget=prefs.budget)
    if costs["known_subtotal_range"]["maximum"] > prefs.budget:
        errors.append("Budget exceeded using conservative upper price bounds")
    if costs["unknown_cost_items"]:
        critical_unknown = True
    # Driving incurs fuel/fare/parking even if Google has no price data.
    if prefs.travel_mode == "driving" and plan.transport_cost.maximum == 0:
        errors.append("Driving transport cost cannot silently be zero")
    if plan.transport_cost.confidence == "verified" and not (
        prefs.travel_mode == "walking" and plan.transport_cost.minimum == plan.transport_cost.maximum == 0
    ):
        errors.append("Verified transport price unsupported by available providers")
    warnings.extend(costs["warnings"])
    if prefs.soft_constraints:
        warnings.append("Crowds and atmosphere cannot be verified; soft preferences are best effort")
    status = "invalid" if errors else ("conditional" if critical_unknown else "valid")
    return ValidationResult(passed=status == "valid", status=status, errors=list(dict.fromkeys(errors)),
                            warnings=list(dict.fromkeys(warnings)), total_minutes=max(0, previous_end-start),
                            cost=costs).model_dump(mode="json")
