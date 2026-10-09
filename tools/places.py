from pydantic import Field

from agent.schemas import Schema
from providers.google import ProviderError


class SearchArguments(Schema):
    query: str = Field(min_length=1, max_length=500)
    city: str = Field(min_length=1, max_length=120)
    area: str | None
    limit: int = Field(ge=1, le=8)


def search_places(query, city, area=None, limit=8, *, google=None, mock=None, osm=None, primary_osm=False):
    warning = None
    if osm:
        try:
            places = osm.search(query, city, area, limit)
            return {"status": "ok" if places else "empty", "source": "openstreetmap",
                    "places": [p.model_dump(mode="json") for p in places],
                    "cached": getattr(osm, "last_cached", False),
                    "warnings": ["© OpenStreetMap contributors · ODbL; prices, ratings and untagged amenities remain unknown.",
                                 "Search uses the submitted city/neighborhood anchor; area text does not trigger extra geocoding."]}
        except ProviderError as exc:
            warning = str(exc)
    elif primary_osm:
        warning = "OSM discovery unavailable because the submitted location could not be resolved"
    if google and not primary_osm and osm is None:
        try:
            places = google.search(query, city, area, limit)
            return {"status": "ok" if places else "empty", "source": "google_places",
                    "places": [p.model_dump(mode="json") for p in places], "warnings": []}
        except ProviderError as exc:
            warning = str(exc)
    places = mock.search(query, city, area, limit) if mock else []
    return {"status": "ok" if places else "empty", "source": "mock",
            "places": [p.model_dump(mode="json") for p in places],
            "warnings": [warning or "Explicit mock mode selected", "Mock Bengaluru fixtures; confirm all venue details"]}
