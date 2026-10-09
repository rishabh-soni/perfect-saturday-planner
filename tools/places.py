from pydantic import Field

from agent.schemas import Schema
from providers.google import ProviderError


class SearchArguments(Schema):
    query: str = Field(min_length=1, max_length=500)
    city: str = Field(min_length=1, max_length=120)
    area: str | None
    limit: int = Field(ge=1, le=8)


def search_places(query, city, area=None, limit=8, *, google=None, mock=None):
    warning = None
    if google:
        try:
            places = google.search(query, city, area, limit)
            return {"status": "ok" if places else "empty", "source": "google_places",
                    "places": [p.model_dump(mode="json") for p in places], "warnings": []}
        except ProviderError as exc:
            warning = str(exc)
    places = mock.search(query, city, area, limit) if mock else []
    return {"status": "ok" if places else "empty", "source": "mock",
            "places": [p.model_dump(mode="json") for p in places],
            "warnings": [warning or "Google unavailable or mock mode selected", "Mock Bengaluru fixtures; confirm all venue details"]}
