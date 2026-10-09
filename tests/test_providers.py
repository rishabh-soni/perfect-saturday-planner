import httpx
import pytest

from providers.google import GoogleProvider, ProviderError
from providers.mock import MockProvider, PLACES
from tools.places import search_places
from tools.routing import get_route


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("code,fragment", [(403, "authentication"), (429, "rate limit"), (500, "unavailable")])
def test_google_failure_falls_back_to_labeled_mocks(code, fragment):
    with client_for(lambda request: httpx.Response(code, json={"secret": "do-not-log"})) as client:
        result = search_places("park", "Bengaluru", google=GoogleProvider("test-key", client), mock=MockProvider())
    assert result["source"] == "mock" and result["places"]
    assert fragment in result["warnings"][0]
    assert "do-not-log" not in str(result)


def test_google_timeout_fallback():
    def timeout(request):
        raise httpx.ReadTimeout("private URL/API key", request=request)
    with client_for(timeout) as client:
        result = search_places("park", "Bengaluru", google=GoogleProvider("key", client), mock=MockProvider())
    assert "timed out" in result["warnings"][0]


def test_empty_google_results_not_replaced_by_mock():
    with client_for(lambda request: httpx.Response(200, json={"places": []})) as client:
        result = search_places("music", "Bengaluru", google=GoogleProvider("key", client), mock=MockProvider())
    assert result["status"] == "empty" and result["source"] == "google_places"


def test_places_request_and_missing_optional_fields():
    def handler(request):
        assert request.url.path == "/v1/places:searchText"
        assert "places.id" in request.headers["X-Goog-FieldMask"]
        assert request.headers["X-Goog-Api-Key"] == "test-key"
        return httpx.Response(200, json={"places": [{"id": "real-id", "displayName": {"text": "Found venue"}, "formattedAddress": "Bengaluru"}]})
    with client_for(handler) as client:
        places = GoogleProvider("test-key", client).search("park", "Bengaluru")
    assert places[0].rating is None and places[0].opening_hours is None
    assert places[0].latitude is None and places[0].estimated_price is None


def test_missing_google_key_fallback():
    result = search_places("park", "Bengaluru", google=GoogleProvider(""), mock=MockProvider())
    assert result["source"] == "mock" and "missing" in result["warnings"][0]


def test_mock_unknown_city_and_music_are_empty():
    assert MockProvider().search("park", "Delhi") == []
    assert MockProvider().search("live music", "Bengaluru") == []


def test_mock_routes_are_not_verified():
    route = MockProvider().route(PLACES[0], PLACES[1], "walking")
    assert route.status == "estimated" and route.confidence == "low"
    assert 10 < route.duration_minutes < 40


def test_route_failure_is_unavailable_without_fabricated_estimate():
    first, last = [p.model_copy(deep=True) for p in PLACES[:2]]
    first.source = last.source = "google_places"
    with client_for(lambda request: httpx.Response(429)) as client:
        result = get_route({"place_id": first.place_id, "latitude": None, "longitude": None},
                           {"place_id": last.place_id, "latitude": None, "longitude": None}, "walking",
                           catalog={first.place_id: first, last.place_id: last}, google=GoogleProvider("key", client), mock=MockProvider())
    assert result["status"] == "unavailable"
    assert result["duration_minutes"] is None and result["distance_meters"] is None


def test_routes_protocol():
    def handler(request):
        import json
        body = json.loads(request.content)
        assert request.url.path == "/directions/v2:computeRoutes"
        assert body["travelMode"] == "WALK"
        assert body["origin"]["placeId"] == PLACES[0].place_id
        assert request.headers["X-Goog-FieldMask"] == "routes.duration,routes.distanceMeters"
        return httpx.Response(200, json={"routes": [{"duration": "600s", "distanceMeters": 800}]})
    with client_for(handler) as client:
        route = GoogleProvider("key", client).route(PLACES[0], PLACES[1], "walking")
    assert route.duration_minutes == 10 and route.status == "available"


@pytest.mark.parametrize("data", [{"routes": []}, {"routes": [{"duration": "nonsense", "distanceMeters": 10}]},
                                  {"routes": [{"duration": "20s", "distanceMeters": -1}]}])
def test_malformed_route_response(data):
    with client_for(lambda request: httpx.Response(200, json=data)) as client:
        with pytest.raises(ProviderError):
            GoogleProvider("key", client).route(PLACES[0], PLACES[1], "walking")


def test_route_unknown_coordinates_rejected():
    with pytest.raises(ValueError):
        get_route({"place_id": None, "latitude": 0, "longitude": 0},
                  {"place_id": PLACES[0].place_id, "latitude": None, "longitude": None}, "walking",
                  catalog={p.place_id: p for p in PLACES}, mock=MockProvider())
