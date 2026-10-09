"""Google Places (New) and Routes REST clients with bounded HTTP timeouts."""
import math
import re

import httpx

from agent.schemas import Place, Route


class ProviderError(Exception):
    """Safe message: deliberately excludes raw HTTP bodies and credentials."""


class GoogleProvider:
    def __init__(self, api_key: str, client: httpx.Client | None = None):
        self.api_key = api_key
        self.client = client

    def _post(self, url, body, mask):
        if not self.api_key:
            raise ProviderError("Google API key is missing")
        headers = {"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": mask}
        try:
            if self.client is None:
                with httpx.Client(timeout=12) as client:
                    response = client.post(url, json=body, headers=headers)
            else:
                response = self.client.post(url, json=body, headers=headers, timeout=12)
            if response.status_code in (401, 403):
                raise ProviderError("Google API authentication or permissions failed")
            if response.status_code == 429:
                raise ProviderError("Google API rate limit exceeded")
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException:
            raise ProviderError("Google API timed out") from None
        except (httpx.HTTPError, ValueError):
            raise ProviderError("Google API unavailable or returned malformed data") from None

    def search(self, query, city, area=None, limit=8):
        mask = ",".join("places." + field for field in (
            "id", "displayName", "formattedAddress", "location", "rating", "regularOpeningHours",
            "priceLevel", "types", "servesVegetarianFood", "accessibilityOptions", "businessStatus"))
        data = self._post("https://places.googleapis.com/v1/places:searchText",
                          {"textQuery": " ".join(filter(None, [query, area, city])), "pageSize": limit}, mask)
        try:
            results = []
            for item in data.get("places", []):
                if item.get("businessStatus") in {"CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"}:
                    continue
                loc = item.get("location", {})
                evidence = {}
                if "servesVegetarianFood" in item:
                    evidence["vegetarian"] = item["servesVegetarianFood"]
                results.append(Place(place_id=item["id"], name=item["displayName"]["text"],
                                     address=item["formattedAddress"], latitude=loc.get("latitude"),
                                     longitude=loc.get("longitude"), rating=item.get("rating"),
                                     opening_hours=item.get("regularOpeningHours"), price_level=item.get("priceLevel"),
                                     source="google_places", types=item.get("types", []), evidence=evidence,
                                     notes=["Regular hours may differ on holidays. Price level is not a menu price; crowd levels are unknown."]))
            return results[:limit]
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ProviderError("Google Places returned incomplete or malformed place data") from None

    def route(self, origin: Place, destination: Place, travel_mode: str):
        def waypoint(place):
            if place.source in {"openstreetmap", "nominatim"}:
                if place.latitude is None or place.longitude is None:
                    raise ProviderError("OSM endpoint coordinates are missing")
                return {"location": {"latLng": {"latitude": place.latitude, "longitude": place.longitude}}}
            return {"placeId": place.place_id}
        data = self._post("https://routes.googleapis.com/directions/v2:computeRoutes",
                          {"origin": waypoint(origin), "destination": waypoint(destination),
                           "travelMode": "WALK" if travel_mode == "walking" else "DRIVE"},
                          "routes.duration,routes.distanceMeters")
        try:
            route = data.get("routes", [])[0]
            duration = route["duration"]
            if not re.fullmatch(r"\d+(?:\.\d+)?s", duration):
                raise ValueError()
            minutes, distance = float(duration[:-1]) / 60, float(route["distanceMeters"])
            if not math.isfinite(minutes) or not math.isfinite(distance) or min(minutes, distance) < 0:
                raise ValueError()
            return Route(origin_id=origin.place_id, destination_id=destination.place_id,
                         travel_mode=travel_mode, distance_meters=distance, duration_minutes=minutes,
                         status="available", source="google_routes", confidence="provider_estimate",
                         warning="Google Maps route estimate; Saturday traffic and delays may differ. Coordinates may snap to roads; confirm venue entrances.")
        except (IndexError, KeyError, TypeError, ValueError, AttributeError):
            raise ProviderError("Google Routes returned no usable route") from None
