from typing import Literal
import math
import os

from providers.osm import distance_meters

from agent.schemas import Place, Route, Schema, Waypoint
from providers.google import ProviderError


class RouteArguments(Schema):
    origin: Waypoint
    destination: Waypoint
    travel_mode: Literal["walking", "driving"]


def resolve_waypoint(waypoint: Waypoint, catalog: dict[str, Place]) -> Place:
    if waypoint.place_id:
        if waypoint.place_id not in catalog:
            raise ValueError("Route endpoint was not discovered through search_places")
        return catalog[waypoint.place_id]
    for place in catalog.values():
        if place.latitude == waypoint.latitude and place.longitude == waypoint.longitude:
            return place
    raise ValueError("Coordinates must match a discovered place")


def get_route(origin, destination, travel_mode, *, catalog, google=None, mock=None, route_cache=None):
    first = resolve_waypoint(Waypoint.model_validate(origin), catalog)
    last = resolve_waypoint(Waypoint.model_validate(destination), catalog)
    if travel_mode not in {"walking", "driving"}:
        raise ValueError("Unsupported travel mode")
    edge = (first.place_id, last.place_id, travel_mode)
    if route_cache is not None and edge in route_cache:
        # Reuse only the current planning run's evidence; no persistent Google cache.
        return route_cache[edge].model_dump(mode="json")
    if first.source == last.source == "mock" and mock:
        return mock.route(first, last, travel_mode).model_dump(mode="json")
    try:
        if not google or first.source not in {"google_places", "openstreetmap", "nominatim"} or last.source not in {"google_places", "openstreetmap", "nominatim"}:
            raise ProviderError("Routing unavailable for these endpoints")
        return google.route(first, last, travel_mode).model_dump(mode="json")
    except ProviderError as exc:
        enabled = os.getenv("ROUTING_FALLBACK", "1").lower() not in {"0", "false", "no"}
        if enabled and first.source in {"openstreetmap", "nominatim"} and last.source in {"openstreetmap", "nominatim"} and all(
                v is not None for v in (first.latitude, first.longitude, last.latitude, last.longitude)):
            direct = distance_meters((first.latitude, first.longitude), (last.latitude, last.longitude))
            if direct <= (2000 if travel_mode == "walking" else 5000):
                distance = math.ceil(direct * 1.6)
                minutes = math.ceil(distance / (50 if travel_mode == "walking" else 166)) + (5 if travel_mode == "walking" else 10)
                return Route(origin_id=first.place_id, destination_id=last.place_id, travel_mode=travel_mode,
                    distance_meters=distance, duration_minutes=minutes, status="estimated", source="distance_estimate", confidence="low",
                    warning=f"{exc}. Straight-line distance ×1.6 and conservative speed/buffer only; not a navigable route or Google estimate. Gates, barriers, roads and safety are unverified.").model_dump(mode="json")
        return Route(origin_id=first.place_id, destination_id=last.place_id,
                     travel_mode=travel_mode, distance_meters=None, duration_minutes=None,
                     status="unavailable", source="unavailable", confidence="unavailable",
                     warning=str(exc)).model_dump(mode="json")
