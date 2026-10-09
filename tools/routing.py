from typing import Literal

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


def get_route(origin, destination, travel_mode, *, catalog, google=None, mock=None):
    first = resolve_waypoint(Waypoint.model_validate(origin), catalog)
    last = resolve_waypoint(Waypoint.model_validate(destination), catalog)
    if travel_mode not in {"walking", "driving"}:
        raise ValueError("Unsupported travel mode")
    if first.source == last.source == "mock" and mock:
        return mock.route(first, last, travel_mode).model_dump(mode="json")
    try:
        if not google or first.source != "google_places" or last.source != "google_places":
            raise ProviderError("Routing unavailable for these endpoints")
        return google.route(first, last, travel_mode).model_dump(mode="json")
    except ProviderError as exc:
        return Route(origin_id=first.place_id, destination_id=last.place_id,
                     travel_mode=travel_mode, distance_meters=None, duration_minutes=None,
                     status="unavailable", source="unavailable", confidence="unavailable",
                     warning=str(exc)).model_dump(mode="json")
