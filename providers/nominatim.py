"""Server-side user-submitted city/neighborhood lookup; never autocomplete."""
import math
import os

from providers.cache import ProviderCache
from providers.google import ProviderError
from providers.public_http import request_json


class NominatimProvider:
    def __init__(self, *, client=None, cache=None):
        self.client, self.cache = client, cache or ProviderCache()
        self.url = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")

    def resolve(self, city, neighborhood=None):
        query = ", ".join(filter(None, [neighborhood, city]))
        key = self.cache.key("nominatim", [self.url, query.casefold()])
        cached = self.cache.get(key)
        if cached is not None:
            if not cached:
                raise ProviderError("Nominatim found no matching city or neighborhood")
            return {**cached, "cached": True}
        with self.cache.request_gate("Nominatim", interval=1.05) as scoped:
            # One shared lock spans the lookup and cache fill. Duplicate requests
            # from concurrent Streamlit sessions reuse the first response.
            cached = scoped.get(key)
            if cached is not None:
                if not cached:
                    raise ProviderError("Nominatim found no matching city or neighborhood")
                return {**cached, "cached": True}
            params = {"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1}
            data = request_json("GET", self.url, client=self.client, cache=scoped, service="Nominatim", params=params)
            try:
                if not isinstance(data, list):
                    raise ValueError()
                if not data:
                    scoped.put(key, {}, 3600)
                    raise ProviderError("Nominatim found no matching city or neighborhood")
                item = data[0]
                lat, lon = float(item["lat"]), float(item["lon"])
                if not math.isfinite(lat+lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError()
                resolved = {"latitude": lat, "longitude": lon, "display_name": item["display_name"],
                            "osm_type": item["osm_type"], "osm_id": item["osm_id"], "source": "nominatim",
                            "attribution": "© OpenStreetMap contributors · ODbL", "cached": False}
                scoped.put(key, resolved, 30*86400)
                return resolved
            except (KeyError, TypeError, ValueError, IndexError):
                raise ProviderError("Nominatim returned incomplete location data") from None
