"""All HTTP mocked, including multi-turn calls through the real OpenAI SDK."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import time
from pathlib import Path
import time as clock

import httpx
import pytest

from agent.planner import Planner
from agent.schemas import Decision, Itinerary, Place, Preferences
from main import EXAMPLE
from providers.cache import ProviderCache
from providers.google import GoogleProvider, ProviderError
from providers.nominatim import NominatimProvider
from providers.osm import OverpassProvider, normalize_element
from providers.weather import OpenMeteoProvider
from tools.osm_hours import osm_opening_check
from tools.places import search_places
from tools.routing import get_route
from tools.validator import validate_plan
from ui.map import map_html
from tests.test_validation import plan, price, stop


def location(city="Bengaluru"):
    return {"lat": "12.97", "lon": "77.59", "display_name": city, "osm_type": "relation", "osm_id": 10}


def elements():
    return [
        {"type": "node", "id": 1, "lat": 12.971, "lon": 77.591,
         "tags": {"name": "Tagged vegetarian cafe", "amenity": "cafe", "diet:vegetarian": "yes", "cuisine": "indian", "opening_hours": "Mo-Su 09:00-20:00", "addr:street": "Museum Road"}},
        {"type": "way", "id": 2, "center": {"lat": 12.972, "lon": 77.592},
         "tags": {"name": "Tagged gallery", "tourism": "gallery", "opening_hours": "Sa 10:00-18:00"}},
        {"type": "relation", "id": 3, "center": {"lat": 12.9701, "lon": 77.5901},
         "tags": {"name": "Tagged park", "leisure": "park", "opening_hours": "24/7"}},
    ]


def forecast_data(date="2026-10-10", rain=80, temperature=25):
    return {"timezone": "Asia/Kolkata", "hourly": {
        "time": [f"{date}T{h:02d}:00" for h in range(10, 15)],
        "precipitation_probability": [rain]*5, "temperature_2m": [temperature]*5, "weather_code": [95 if rain >= 80 else 1]*5}}


@pytest.fixture
def cache(tmp_path):
    return ProviderCache(tmp_path / "cache.sqlite3")


def test_osm_node_way_relation_metadata_and_missing_values():
    places = [normalize_element(e) for e in elements()]
    assert [p.place_id for p in places] == ["osm:node:1", "osm:way:2", "osm:relation:3"]
    assert places[1].latitude == 12.972 and places[2].longitude == 77.5901
    assert places[0].cuisine == "indian" and places[0].evidence["vegetarian"]
    assert places[0].address == "Museum Road"
    assert places[1].address == "Address not tagged in OpenStreetMap"
    assert all(p.rating is None and p.estimated_price is None for p in places)
    assert places[1].evidence == {} and places[1].opening_hours["osm_expression"] == "Sa 10:00-18:00"
    assert normalize_element({"type": "way", "id": 5, "tags": {"name": "No center"}}) is None
    assert normalize_element({"type": "node", "id": 6, "lat": 1, "lon": 2, "tags": {}}) is None


def test_overpass_query_cache_and_weather_ranking(cache):
    seen = []
    def handler(request):
        seen.append(request)
        query = request.content.decode()
        assert "nwr" in query and "center" in query and "2500" in query
        assert "User-Agent" in request.headers and "PerfectSaturdayPlanner" in request.headers["User-Agent"]
        return httpx.Response(200, json={"elements": elements()})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = OverpassProvider(anchor={"latitude": 12.97, "longitude": 77.59}, client=client, cache=cache)
        clear = provider.search("food parks galleries", "Bengaluru")
        assert clear[0].environment == "outdoor"  # Closest to anchor.
        provider.prefer_indoor = True
        rainy = provider.search("galleries parks food", "Bengaluru")
        assert rainy[0].environment == "indoor" and rainy[-1].environment == "outdoor"
        assert len(seen) == 1 and provider.last_cached


@pytest.mark.parametrize("status", [403, 429, 504])
def test_overpass_errors_are_safe_and_back_off(cache, status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"secret": "do-not-log"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = OverpassProvider(anchor={"latitude": 1, "longitude": 2}, client=client, cache=cache)
        first = search_places("park", "Jaipur", osm=provider, primary_osm=True)
        second = search_places("park", "Jaipur", osm=provider, primary_osm=True)
    assert not first["places"] and len(calls) == 1
    assert "do-not-log" not in str(first) and "cooling down" in str(second)


def test_empty_osm_does_not_fake_live_places(cache):
    from providers.mock import MockProvider
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"elements": []}))) as client:
        provider = OverpassProvider(anchor={"latitude": 1, "longitude": 2}, client=client, cache=cache)
        result = search_places("park", "Bengaluru", osm=provider, primary_osm=True, mock=MockProvider())
    assert result["status"] == "empty" and result["source"] == "openstreetmap" and not result["places"]


@pytest.mark.parametrize("payload", [{"elements": [], "remark": "query timed out"}, {"elements": "wrong"}, []])
def test_overpass_incomplete_response_not_accepted(cache, payload):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as client:
        with pytest.raises(ProviderError):
            OverpassProvider(anchor={"latitude": 1, "longitude": 2}, client=client, cache=cache).search("parks", "Bengaluru")


def test_nominatim_cache_and_one_application_wide_gate(tmp_path):
    now = [100.0]
    sleep = lambda seconds: now.__setitem__(0, now[0]+seconds)
    shared = tmp_path / "shared.sqlite3"
    a = ProviderCache(shared, clock=lambda: now[0], sleep=sleep)
    b = ProviderCache(shared, clock=lambda: now[0], sleep=sleep)
    starts = []
    def handler(request):
        starts.append(now[0])
        assert request.url.params["format"] == "jsonv2" and request.url.params["limit"] == "1"
        assert request.headers["User-Agent"].startswith("PerfectSaturdayPlanner/")
        return httpx.Response(200, json=[location()])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first, second = NominatimProvider(client=client, cache=a), NominatimProvider(client=client, cache=b)
        assert first.resolve("Bengaluru")["cached"] is False
        assert second.resolve("Bengaluru")["cached"] is True
        second.resolve("Jaipur")
    assert len(starts) == 2 and starts[1]-starts[0] >= 1.049


def test_concurrent_separate_cache_instances_share_rate_limit(tmp_path):
    path = tmp_path / "gate.sqlite3"
    instances = [ProviderCache(path), ProviderCache(path)]
    def reserve(index):
        instances[index].reserve("Nominatim", interval=1.05)
        return clock.time()
    with ThreadPoolExecutor(max_workers=2) as pool:
        starts = sorted(pool.map(reserve, [0, 1]))
    assert starts[1]-starts[0] >= 1.0


@pytest.mark.parametrize("payload", [[], [{"lat": "nan", "lon": "1"}], {}])
def test_nominatim_empty_and_malformed_are_safe(cache, payload):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as client:
        with pytest.raises(ProviderError):
            NominatimProvider(client=client, cache=cache).resolve("Bengaluru")


def test_public_timeout_does_not_expose_raw_input(cache):
    def handler(request):
        raise httpx.ReadTimeout("private-key", request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="timed out") as error:
            NominatimProvider(client=client, cache=cache).resolve("Bengaluru")
    assert "private-key" not in str(error.value)


def test_forecast_window_partial_hours_and_cache(cache):
    requests = []
    payload = forecast_data()
    payload["hourly"]["temperature_2m"][1] = None
    def handler(request):
        requests.append(request)
        assert request.url.params["timezone"] == "auto"
        assert request.url.params["hourly"] == "precipitation_probability,temperature_2m,weather_code"
        return httpx.Response(200, json=payload)
    prefs = Preferences.model_validate({**EXAMPLE, "saturday": "2026-10-10", "start_time": "10:30", "available_time": "2 hours"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = OpenMeteoProvider(client=client, cache=cache)
        result = provider.forecast({"latitude": 1, "longitude": 2}, prefs)
        second = provider.forecast({"latitude": 1, "longitude": 2}, prefs)
    assert result["status"] == "partial" and result["prefer_indoor"]
    assert [h["time"][11:16] for h in result["hourly"]] == ["10:00", "11:00", "12:00"]
    assert second["cached"] and len(requests) == 1


@pytest.mark.parametrize("payload,status", [({"reason": "Date outside forecast horizon"}, 400),
    ({"hourly": {"time": [], "precipitation_probability": [], "temperature_2m": [], "weather_code": []}}, 200)])
def test_weather_unavailable_is_not_sunshine(cache, payload, status):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=payload))) as client:
        result = OpenMeteoProvider(client=client, cache=cache).forecast({"latitude": 1, "longitude": 2}, Preferences.model_validate(EXAMPLE))
    assert result["status"] == "unavailable" and not result["hourly"]
    assert "do not assume dry weather" in result["guidance"]


@pytest.mark.parametrize("expression,start,end,expected", [
    ("Mo-Su 09:00-18:00; Sa 10:00-16:00", 570, 600, False),
    ("24/7", 0, 60, True), ("Mo-Fr 09:00-18:00", 600, 660, False),
    ("Mo-Su 09:00-18:00", 600, 660, True), ("Sa 10:00-12:00,13:00-18:00", 690, 810, False),
    ("Fr 22:00-02:00", 30, 90, True), ("Mo-Su 09:00-18:00; Sa off", 600, 660, False),
    ("Mo-Su 09:00-18:00; PH off", 600, 660, None), ("sunrise-sunset", 600, 660, None),
    ("Sa 09:00-12:00,12:00-18:00", 690, 810, True)])
def test_osm_hours_conservative_subset(expression, start, end, expected):
    assert osm_opening_check(expression, start, end) is expected


def test_weather_and_osm_hours_reach_mandatory_validation():
    park = normalize_element(elements()[2])
    prefs = Preferences.model_validate({**EXAMPLE, "saturday": "2026-10-10"})
    weather = {"status": "available", "hourly": [{"time": "2026-10-10T10:00", "temperature_c": 25, "precipitation_probability": 90, "weather_code": 95}]}
    result = validate_plan(plan([stop(park.place_id)]), prefs.model_dump(mode="json"), catalog={park.place_id: park}, weather=weather)
    assert result["status"] == "conditional" and any("Adverse weather" in w for w in result["warnings"])
    park.opening_hours = {"osm_expression": "Sa 12:00-18:00"}
    result = validate_plan(plan([stop(park.place_id)]), prefs.model_dump(mode="json"), catalog={park.place_id: park}, weather=weather)
    assert result["errors"] and any("outside" in e for e in result["errors"])


@pytest.mark.parametrize("mode", ["walking", "driving"])
def test_google_routes_osm_coordinates_minimal_mask_and_fallback(cache, mode):
    first, last = [normalize_element(e) for e in elements()[:2]]
    catalog = {p.place_id: p for p in (first, last)}
    args = {"origin": {"place_id": first.place_id, "latitude": None, "longitude": None},
            "destination": {"place_id": last.place_id, "latitude": None, "longitude": None}, "travel_mode": mode, "catalog": catalog}
    seen = []
    def handler(request):
        seen.append(request)
        body = json.loads(request.content)
        assert body["origin"]["location"]["latLng"]["latitude"] == first.latitude
        assert "placeId" not in body["origin"]
        assert body["travelMode"] == ("WALK" if mode == "walking" else "DRIVE")
        assert request.headers["X-Goog-FieldMask"] == "routes.duration,routes.distanceMeters"
        return httpx.Response(200, json={"routes": [{"duration": "600s", "distanceMeters": 700}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        route = get_route(**args, google=GoogleProvider("test", client))
        from agent.schemas import Route
        again = get_route(**args, google=GoogleProvider("test", client), route_cache={(first.place_id, last.place_id, mode): Route.model_validate(route)})
    assert route["source"] == "google_routes" and again == route and len(seen) == 1
    fallback = get_route(**args)
    assert fallback["source"] == "distance_estimate" and fallback["status"] == "estimated"
    assert "not a navigable route" in fallback["warning"]


def test_fallback_refuses_far_or_missing_coordinate_routes(monkeypatch):
    first, last = [normalize_element(e) for e in elements()[:2]]
    last.latitude = 14
    kwargs = {"origin": {"place_id": first.place_id, "latitude": None, "longitude": None},
              "destination": {"place_id": last.place_id, "latitude": None, "longitude": None},
              "travel_mode": "walking", "catalog": {p.place_id: p for p in (first, last)}}
    assert get_route(**kwargs)["status"] == "unavailable"
    last.latitude = None
    assert get_route(**kwargs)["duration_minutes"] is None
    last.latitude = first.latitude
    monkeypatch.setenv("ROUTING_FALLBACK", "0")
    assert get_route(**kwargs)["status"] == "unavailable"


@pytest.mark.parametrize("city", ["Bengaluru", "Jaipur"])
def test_assignment_native_sdk_agent_uses_osm_weather_routes(cache, city):
    from openai import OpenAI
    prefs = Preferences.model_validate({**EXAMPLE, "city": city, "saturday": "2026-10-10"})
    unknown = price(None, None, "unknown")
    draft = plan([stop("osm:node:1", end="10:50", cost=unknown, kind="food"),
                  stop("osm:way:2", start="11:10", end="11:50", cost=unknown, kind="culture")])
    draft["trade_offs"] = ["Indoor gallery replaces the outdoor walk because retrieved precipitation probability is 80%."]
    requests, turns = [], []
    def handler(request):
        requests.append(request)
        if request.url.host == "nominatim.openstreetmap.org":
            return httpx.Response(200, json=[location(city)])
        if request.url.host == "overpass-api.de":
            return httpx.Response(200, json={"elements": elements()})
        if request.url.host == "api.open-meteo.com":
            return httpx.Response(200, json=forecast_data())
        if request.url.host == "routes.googleapis.com":
            return httpx.Response(200, json={"routes": [{"duration": "300s", "distanceMeters": 300}]})
        assert request.url.host == "api.openai.com"
        body = json.loads(request.content)
        turns.append(body)
        if len(turns) == 1:
            context = json.loads(body["messages"][1]["content"])
            assert context["weather"]["prefer_indoor"] and context["location"]["display_name"] == city
            calls = [("search_places", {"city": "wrong city", "query": "vegetarian food indoor museums parks", "area": None, "limit": 8}, "search")]
        elif len(turns) == 2:
            found = json.loads(body["messages"][-1]["content"])["result"]
            assert found["source"] == "openstreetmap" and found["weather"]["hourly"]
            assert found["places"][0]["environment"] == "indoor"
            calls = [("get_weather", {}, "weather"), ("get_route", {
                "origin": {"place_id": "osm:node:1", "latitude": None, "longitude": None},
                "destination": {"place_id": "osm:way:2", "latitude": None, "longitude": None}, "travel_mode": "walking"}, "route"),
                ("estimate_cost", {"activities": [{"label": "Meal", "cost": unknown}], "transport_cost": price(confidence="verified")}, "cost"),
                ("validate_plan", {"itinerary": draft, "preferences": prefs.model_dump(mode="json")}, "validate")]
        else:
            responses = {m["tool_call_id"]: json.loads(m["content"])["result"] for m in body["messages"] if m["role"] == "tool"}
            assert responses["weather"]["hourly"][0]["precipitation_probability"] == 80
            assert responses["validate"]["errors"] == []
            calls = [("submit_plan", Decision(status="recommendation", message="An indoor food and art plan for rainy weather", itinerary=Itinerary.model_validate(draft)).model_dump(mode="json"), "final")]
        return httpx.Response(200, json={"id": "chat-test", "object": "chat.completion", "created": 0,
            "model": body["model"], "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{"type": "function", "id": i,
                "function": {"name": name, "arguments": json.dumps(args)}} for name, args, i in calls]}}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        with OpenAI(api_key="test-not-real", max_retries=0, http_client=http_client) as client:
            planner = Planner(client=client, cache=cache, google=GoogleProvider("test", http_client),
                geocoder=NominatimProvider(client=http_client, cache=cache),
                osm=OverpassProvider(client=http_client, cache=cache), weather=OpenMeteoProvider(client=http_client, cache=cache))
            result = planner.plan(prefs.model_dump(mode="json"))
    assert result.status == "conditional" and not result.validation.errors, result.model_dump_json()
    assert result.weather["source"] == "open_meteo" and len(turns) == 3
    assert all(s["venue"]["source"] == "openstreetmap" for s in result.itinerary["stops"])
    assert {"search_places", "get_weather", "get_route", "estimate_cost", "validate_plan"} <= planner.executed_tools
    assert all(r.url.host != "places.googleapis.com" for r in requests)


def test_map_escape_attribution_schematic_and_missing_coordinates(monkeypatch):
    first, last = [normalize_element(e) for e in elements()[:2]]
    first.name = '<script>alert("x")</script> `${alert(2)}'
    data = {"stops": [{**stop(p.place_id), "venue": p.model_dump(mode="json")} for p in (first, last)]}
    page = map_html(data)
    assert "OpenStreetMap contributors" in page and "https://tile.openstreetmap.org/" in page
    assert "Schematic stop order" in page and "not a road or walking route" in page
    assert '<script>alert("x")</script>' not in page and "keepBuffer" in page
    assert "${alert(2)}" not in page and "&#36;{alert(2)}" in page
    assert "Google Maps" not in page and "googleapis.com" not in page
    assert map_html({"stops": []}) is None
    monkeypatch.setenv("MAP_TILE_URL", "https://maps.google.com/tiles/{z}/{x}/{y}")
    with pytest.raises(ValueError):
        map_html(data)


def test_private_osm_venue_is_not_suggested():
    item = elements()[0]
    item["tags"]["access"] = "private"
    assert normalize_element(item) is None


def test_neighborhood_origin_is_geocoded_and_never_an_activity(cache):
    class Geocoder:
        def resolve(self, city, neighborhood=None):
            return {"latitude": 12.97, "longitude": 77.59, "display_name": "Neighborhood reference" if neighborhood else city,
                    "osm_type": "relation", "osm_id": 100 if neighborhood else 10}
    class Weather:
        def forecast(self, anchor, prefs):
            return {"status": "unavailable", "hourly": [], "prefer_indoor": False, "guidance": "Forecast unavailable"}
    from tests.test_agent import ScriptedResponses, call, final, budget_step
    prefs = Preferences.model_validate({**EXAMPLE, "starting_neighborhood": "Indiranagar"})
    draft = plan([stop("osm:node:1", start="10:30", end="11:10", kind="food", cost=price(None, None, "unknown"))])
    draft["starting_place_id"] = "origin:relation:100"
    calls = [call("search_places", {"query": "vegetarian food", "city": "Bengaluru", "area": None, "limit": 8}, "search"),
             call("get_route", {"origin": {"place_id": "origin:relation:100", "latitude": None, "longitude": None},
                                "destination": {"place_id": "osm:node:1", "latitude": None, "longitude": None}, "travel_mode": "walking"}, "route"),
             budget_step(), final(draft)]
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"elements": elements()}))) as client:
        planner = Planner(llm=ScriptedResponses(calls), geocoder=Geocoder(), weather=Weather(),
                          osm=OverpassProvider(client=client, cache=cache), cache=cache)
        result = planner.plan(prefs.model_dump(mode="json"))
    assert result.status == "conditional" and not result.validation.errors
    assert result.itinerary["routes"][0]["source"] == "distance_estimate"
    origin = planner.catalog["origin:relation:100"]
    assert origin.resolved_neighborhood == "Indiranagar"
    assert any(e.name == "resolve_starting_neighborhood" for e in result.trace)
    bad = plan([stop(origin.place_id)])
    checked = validate_plan(bad, Preferences.model_validate(EXAMPLE).model_dump(mode="json"), catalog=planner.catalog)
    assert any("not a discovered activity venue" in e for e in checked["errors"])


def test_unresolved_neighborhood_does_not_invent_origin(cache):
    class Geocoder:
        def resolve(self, city, neighborhood=None):
            if neighborhood:
                raise ProviderError("Nominatim found no matching neighborhood")
            return {"latitude": 12.97, "longitude": 77.59, "display_name": city, "osm_type": "relation", "osm_id": 10}
    from tests.test_agent import ScriptedResponses
    llm = ScriptedResponses([])
    planner = Planner(llm=llm, geocoder=Geocoder(), cache=cache)
    result = planner.plan({**EXAMPLE, "starting_neighborhood": "Not a real neighborhood"})
    assert result.status == "failure" and result.itinerary is None and not llm.requests
    assert any(e.name == "resolve_starting_neighborhood" and not e.success for e in result.trace)


def test_unknown_weather_does_not_prevent_conditional_indoor_plan(cache):
    class Geocoder:
        def resolve(self, city, neighborhood=None):
            return {"latitude": 12.97, "longitude": 77.59, "display_name": city, "osm_type": "relation", "osm_id": 10}
    class Weather:
        def forecast(self, anchor, prefs):
            return {"status": "unavailable", "hourly": [], "prefer_indoor": False, "guidance": "Keep an indoor backup"}
    from tests.test_agent import ScriptedResponses, call, final, budget_step
    draft = plan([stop("osm:way:2", kind="culture", cost=price(None, None, "unknown"))])
    steps = [call("search_places", {"query": "museum", "city": "Bengaluru", "area": None, "limit": 8}, "search"), budget_step(), final(draft)]
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"elements": elements()}))) as client:
        planner = Planner(llm=ScriptedResponses(steps), geocoder=Geocoder(), weather=Weather(),
                          osm=OverpassProvider(client=client, cache=cache), cache=cache)
        result = planner.plan(EXAMPLE)
    assert result.status == "conditional" and any("Weather forecast unavailable" in w for w in result.validation.warnings)


def test_concurrent_nominatim_queries_are_single_flight(tmp_path):
    path = tmp_path / "single-flight.sqlite3"
    caches = [ProviderCache(path), ProviderCache(path)]
    calls = []
    def handler(request):
        calls.append(clock.time())
        clock.sleep(0.05)
        return httpx.Response(200, json=[location()])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        providers = [NominatimProvider(client=client, cache=c) for c in caches]
        with ThreadPoolExecutor(max_workers=2) as pool:
            answers = list(pool.map(lambda i: providers[i].resolve("Bengaluru"), [0, 1]))
    assert len(calls) == 1 and all(a["latitude"] == 12.97 for a in answers)


def test_nominatim_rate_limit_cooldown_persists_across_instances(cache):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="rate limit"):
            NominatimProvider(client=client, cache=cache).resolve("Bengaluru")
        with pytest.raises(ProviderError, match="cooling down"):
            NominatimProvider(client=client, cache=ProviderCache(cache.path)).resolve("Jaipur")
    assert len(calls) == 1
