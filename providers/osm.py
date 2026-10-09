"""Targeted Overpass discovery around a geocoded anchor, with honest metadata."""
import math
import os
import re

from agent.schemas import Place
from providers.cache import ProviderCache
from providers.google import ProviderError
from providers.public_http import request_json


CATEGORIES = {
    "food": ('amenity', 'restaurant|cafe', ["restaurant", "food"]),
    "outdoor": ('leisure', 'park|garden', ["park", "walks", "nature"]),
    "culture": ('tourism', 'museum|gallery|attraction', ["culture", "art"]),
    "books": ('shop', 'books', ["books", "book_store"]),
}


def categories_for(query):
    words = set(re.findall(r"\w+", query.casefold()))
    selected = []
    if words & {"food", "restaurant", "restaurants", "cafe", "cafes", "café", "cafés", "vegetarian", "vegan", "lunch", "meal"}:
        selected.append("food")
    if words & {"walk", "walks", "park", "parks", "garden", "gardens", "nature", "outdoor"}:
        selected.append("outdoor")
    if words & {"museum", "museums", "gallery", "galleries", "art", "culture", "history", "attraction", "attractions", "indoor"}:
        selected.append("culture")
    if words & {"books", "book", "bookshop", "bookstore"}:
        selected.append("books")
    return selected or ["food", "outdoor", "culture"]


def distance_meters(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(h)))


def normalize_element(item):
    tags = item.get("tags") or {}
    if tags.get("access") in {"private", "no"}:
        return None
    name = tags.get("name") or tags.get("name:en")
    if not isinstance(name, str) or not name.strip():
        return None  # Never invent an unnamed venue.
    coordinates = item if item.get("type") == "node" else item.get("center") or {}
    lat, lon = coordinates.get("lat"), coordinates.get("lon")
    if lat is None or lon is None:
        return None
    amenity, tourism, leisure = tags.get("amenity"), tags.get("tourism"), tags.get("leisure")
    if amenity in {"restaurant", "cafe"}:
        types = [amenity, "food"]
        environment = "indoor"
    elif leisure in {"park", "garden"}:
        types, environment = [leisure, "park", "walks", "nature"], "outdoor"
    elif tourism in {"museum", "gallery"}:
        types, environment = [tourism, "culture", "art"], "indoor"
    elif tags.get("shop") == "books":
        types, environment = ["book_store", "books", "culture"], "indoor"
    else:
        types, environment = [tourism or "attraction", "culture"], "unknown"
    if tags.get("indoor") in {"yes", "no"}:
        environment = "indoor" if tags["indoor"] == "yes" else "outdoor"
    evidence = {}
    for constraint, tag in [("vegetarian", "diet:vegetarian"), ("vegan", "diet:vegan"), ("wheelchair", "wheelchair")]:
        if tags.get(tag) in {"yes", "only", "no"}:
            evidence[constraint] = tags[tag] != "no"
    if tags.get("diet:vegetarian") == "only":
        evidence["pure_vegetarian"] = True
    if tags.get("diet:vegan") in {"yes", "only"}:
        evidence["vegetarian"] = True
    address_parts = [tags.get("addr:housenumber"), tags.get("addr:street"), tags.get("addr:suburb"),
                     tags.get("addr:city"), tags.get("addr:postcode")]
    address = tags.get("addr:full") or ", ".join(p for p in address_parts if p) or "Address not tagged in OpenStreetMap"
    return Place(place_id=f"osm:{item['type']}:{item['id']}", name=name, address=address,
        latitude=lat, longitude=lon, source="openstreetmap", types=list(dict.fromkeys(types)), evidence=evidence,
        cuisine=tags.get("cuisine"), osm_tags={k: str(tags[k]) for k in
            ("opening_hours", "cuisine", "diet:vegetarian", "diet:vegan", "wheelchair", "fee", "indoor", "access") if k in tags},
        environment=environment, opening_hours={"osm_expression": tags["opening_hours"]} if tags.get("opening_hours") else None,
        notes=["© OpenStreetMap contributors · ODbL. Community tags may be incomplete or outdated.",
               "Indoor/outdoor suitability is a venue-type heuristic unless an indoor tag is present; confirm access and shelter.",
               "No current prices, ratings or crowd information supplied; public access and venue entrances need confirmation."])


class OverpassProvider:
    def __init__(self, *, anchor=None, client=None, cache=None, radius=None):
        self.anchor, self.client, self.cache = anchor, client, cache or ProviderCache()
        self.url = os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
        try:
            self.radius = min(5000, max(500, int(radius or os.getenv("OSM_SEARCH_RADIUS_METERS", "2500"))))
        except ValueError:
            self.radius = 2500
        self.prefer_indoor = False
        self.last_cached = False

    def search(self, query, city, area=None, limit=8):
        if not self.anchor:
            raise ProviderError("OSM discovery needs a resolved city or starting neighborhood")
        lat, lon = self.anchor["latitude"], self.anchor["longitude"]
        categories = sorted(categories_for(query))
        key = self.cache.key("overpass", [self.url, lat, lon, self.radius, categories])
        data = self.cache.get(key)
        self.last_cached = data is not None
        if data is None:
            self.cache.reserve("Overpass", interval=1)
            selectors = []
            for category in categories:
                tag, values, _ = CATEGORIES[category]
                selectors.append(f'nwr(around:{self.radius},{lat},{lon})["{tag}"~"^({values})$"]["name"];')
            ql = '[out:json][timeout:18];(' + ''.join(selectors) + ');out center tags;'
            data = request_json("POST", self.url, client=self.client, cache=self.cache, service="Overpass",
                                timeout=22, data={"data": ql})
            if not isinstance(data, dict) or not isinstance(data.get("elements"), list) or data.get("remark"):
                self.cache.block("Overpass", 30)
                raise ProviderError("Overpass returned an incomplete query or malformed response")
            self.cache.put(key, data, 3600 if data["elements"] else 300)
        results, seen = [], set()
        for item in data["elements"]:
            try:
                place = normalize_element(item)
                if place and place.place_id not in seen:
                    results.append(place)
                    seen.add(place.place_id)
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        # Compact anchors; dietary tags and bad weather affect ranking, not invented evidence.
        diet = "vegan" if "vegan" in query.casefold() else "vegetarian" if "vegetarian" in query.casefold() else None
        results.sort(key=lambda p: (self.prefer_indoor and p.environment != "indoor",
                                   diet is not None and "food" in p.types and p.evidence.get(diet) is not True,
                                   distance_meters((lat, lon), (p.latitude, p.longitude))))
        return results[:limit]
