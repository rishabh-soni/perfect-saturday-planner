"""Small illustrative dataset, NOT current venue/hours/menu information."""
import math
import re

from agent.schemas import Place, Price, Route


def fixture(identifier, name, address, lat, lng, types, price, evidence=None):
    return Place(place_id=f"mock:{identifier}", name=name, address=address,
                 latitude=lat, longitude=lng, source="mock", types=types,
                 evidence=evidence or {}, estimated_price=Price(
                     minimum=price[0], maximum=price[1], confidence="estimated",
                     basis="Illustrative mock allowance per person; confirm current prices"),
                 notes=["Mock fixture: approximate location; hours, menus and crowds are not verified."])


PLACES = [
    fixture("cubbon", "Cubbon Park", "Kasturba Road, central Bengaluru", 12.9763, 77.5929,
            ["park", "walks", "relax"], (0, 0)),
    fixture("koshys", "Koshy's", "St. Mark's Road, central Bengaluru", 12.9758, 77.6012,
            ["restaurant", "food", "relax"], (350, 650), {"vegetarian": True}),
    fixture("gallery", "Venkatappa Art Gallery", "Kasturba Road, central Bengaluru", 12.9747, 77.5956,
            ["museum", "culture", "art", "relax"], (20, 100)),
    fixture("blossoms", "Blossom Book House", "Church Street, central Bengaluru", 12.9750, 77.6047,
            ["book_store", "books", "culture", "relax"], (0, 0)),
    fixture("vidyarthi", "Vidyarthi Bhavan", "Gandhi Bazaar, Basavanagudi, Bengaluru", 12.9451, 77.5681,
            ["restaurant", "food"], (120, 250), {"vegetarian": True}),
    fixture("lalbagh", "Lalbagh Botanical Garden", "Lalbagh Road, Bengaluru", 12.9507, 77.5848,
            ["park", "walks", "nature"], (30, 100)),
]


class MockProvider:
    def search(self, query, city, area=None, limit=8):
        if city.casefold() not in {"bangalore", "bengaluru"}:
            return []
        words = set(re.findall(r"\w+", query.lower())) - {"in", "near", "bengaluru", "bangalore", "quiet", "vegetarian", "places"}
        synonyms = {"walk": "walks", "cafe": "food", "cafes": "food", "restaurants": "food", "parks": "park", "bookshop": "books"}
        words |= {synonyms[w] for w in words if w in synonyms}
        results = []
        for place in PLACES:
            tokens = set(re.findall(r"\w+", " ".join([place.name, place.address, *place.types]).lower()))
            if words and not words & tokens:
                continue
            if area and area.lower() not in (place.address + " " + place.name).lower():
                continue
            results.append(place.model_copy(deep=True))
        return results[:limit]

    def route(self, origin: Place, destination: Place, travel_mode: str) -> Route:
        coords = (origin.latitude, origin.longitude, destination.latitude, destination.longitude)
        if any(v is None for v in coords):
            return Route(origin_id=origin.place_id, destination_id=destination.place_id,
                         travel_mode=travel_mode, distance_meters=None, duration_minutes=None,
                         status="unavailable", source="unavailable", confidence="unavailable",
                         warning="Mock fixture is missing coordinates")
        lat1, lon1, lat2, lon2 = map(math.radians, coords)
        hav = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
        distance = 6371000 * 2 * math.asin(min(1, math.sqrt(hav))) * 1.4
        minutes = distance / (65 if travel_mode == "walking" else 220)
        return Route(origin_id=origin.place_id, destination_id=destination.place_id,
                     travel_mode=travel_mode, distance_meters=round(distance),
                     duration_minutes=round(max(1, minutes), 1), status="estimated", source="mock",
                     confidence="low", warning="Illustrative straight-line distance x1.4; no road, gate, traffic or safety verification")
