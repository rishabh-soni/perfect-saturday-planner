# Perfect Saturday Planner — Saturday, Sorted

A Streamlit web app that turns a city, budget, time window, mood, interests and constraints into a practical Saturday itinerary. OpenAI selects Python tools through genuine multi-turn function calling. OpenStreetMap supplies venues, Open-Meteo supplies weather, and Google Routes supplies walking/driving estimates.

**Status:** local development and verification complete; no public deployment yet. The local URL is not the hosted submission URL. Deployment is deferred as requested.

## Local setup

Prerequisites: Python **3.11+**, Git, and internet access for installation and live planning. The offline Bengaluru demo requires no API keys or planning API calls; viewing the interactive map still loads browser tiles and Leaflet assets.

### Windows PowerShell

```powershell
git clone https://github.com/rishabh-soni/perfect-saturday-planner.git
cd perfect-saturday-planner
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env as described below, or use the demo without keys.
.\.venv\Scripts\python.exe -m streamlit run app.py
```

These commands use the virtual environment directly, so PowerShell activation is unnecessary. When updating an existing checkout, keep your existing `.env` instead of overwriting it.

### macOS / Linux

```sh
git clone https://github.com/rishabh-soni/perfect-saturday-planner.git
cd perfect-saturday-planner
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# Edit .env as described below, or use the demo without keys.
python -m streamlit run app.py
```

Open the URL printed by Streamlit, normally **http://localhost:8501**. Select **Try the Bengaluru demo** for the fixture-based experience. Without an OpenAI key, the UI automatically selects the demo. For live planning, configure the key and turn the demo toggle off.

## Configuration

Put credentials in the ignored `.env` file. Hosted Streamlit secrets are also supported, but hosting is not performed by this change. Never commit actual keys.

```dotenv
OPENAI_API_KEY=your_openai_key
OPENAI_MODEL=gpt-4.1-mini
GOOGLE_MAPS_API_KEY=your_google_routes_key
```

OpenAI is the active LLM provider. The Google key needs **Routes API** enabled, billing and appropriate API restrictions; **Google Places is not required**. OpenAI and Google Routes can incur charges. Missing/unavailable Google routing can use the explicitly labeled short-distance fallback described below.

| Variable | Purpose / default |
| --- | --- |
| `OPENAI_API_KEY` | Required for live LLM planning; absent keys select the UI demo. |
| `OPENAI_MODEL` | `gpt-4.1-mini`; choose an available OpenAI model supporting function calling. |
| `GOOGLE_MAPS_API_KEY` | Optional Google Routes credential. |
| `OPENAI_DIAGNOSTICS` | `0`; `1` enables redacted diagnostics in traces and ignored local logs. |
| `AGENT_MAX_ITERATIONS` | `12`, bounded to 3–20 model turns. |
| `SATURDAY_DEMO_ONLY` | `1` forces the UI demo; unset for normal operation. |
| `OSM_USER_AGENT` | Descriptive application identifier/contact; see `.env.example`. |
| `NOMINATIM_URL` | `https://nominatim.openstreetmap.org/search` |
| `OVERPASS_URL` | `https://overpass-api.de/api/interpreter` |
| `OSM_SEARCH_RADIUS_METERS` | `2500`, bounded to 500–5000 m around the submitted location. |
| `OPEN_METEO_URL` | `https://api.open-meteo.com/v1/forecast` |
| `PROVIDER_CACHE_PATH` | `artifacts/provider-cache.sqlite3`; share across all workers on one host. |
| `ROUTING_FALLBACK` | `1`; set `0` to require provider routing. |
| `APP_PUBLIC_URL` | `http://127.0.0.1:8501/`; set the actual origin before hosting. |
| `MAP_TILE_URL` | `https://tile.openstreetmap.org/{z}/{x}/{y}.png` |
| `MAP_TILE_ATTRIBUTION` | Visible tile-provider/OSM attribution; see `.env.example`. |

OSM and Open-Meteo integrations need no API keys. If you change a public endpoint or tile provider, follow that provider's terms and update attribution.

## Using the app

The easier testing defaults are Bangalore, INR 2,000, four hours from **11:00**, a relaxed mood, food and walks, walking travel, vegetarian and avoid-crowds preferences. **The assignment example** preset restores the original tired mood, food/music/walks and 10:00 start.

The optional starting neighborhood is resolved to a reference point, not a precise home address. If omitted, the available window begins at the first venue; home and return travel are excluded. Submit the form to apply changes. **Plan again** reuses the last submitted preferences and mode.

Up to three distinct checked choices are offered. Shorter alternatives explain omitted interests and retained constraints. Selecting a choice updates the itinerary, warnings, budget and JSON download. If evidence supports fewer choices, the UI says so instead of inventing options. Unknown costs remain unknown; the budget table shows itemized ranges, bases and the known subtotal separately.

The map shows venue markers and dotted **schematic stop order**, not navigable route geometry. Route times, distances and sources appear separately. The weather panel shows retrieved city-local hourly values. **How your Saturday was planned** displays actual backend actions and sanitized diagnostics.

## Input and output

The original assignment input is supported and included in `examples/input.json`:

```json
{
  "city": "Bangalore",
  "budget": 2000,
  "available_time": "4 hours",
  "mood": "tired but wants to do something fun",
  "interests": ["food", "music", "walks"],
  "constraints": ["vegetarian", "avoid crowded places"]
}
```

Optional fields: `starting_neighborhood`, `start_time` (`HH:MM`), `travel_mode` (`walking` or `driving`) and `saturday` (`YYYY-MM-DD`, a Saturday). Dates default to the next Saturday in India time; schedules and weather use city-local times. The window must finish the same day. Duration accepts `4 hours`, `90 minutes`, `1.5h` or `2h 30m`. **All money is INR**, including for other cities; currency conversion is not implemented.

Output includes original normalized preferences, provider-backed venues, scheduled stops, costs, rationale, routes, weather, validation, alternatives, warnings, model iteration/revision counts and execution trace.

- `success`: supported feasibility checks pass; estimates can still change.
- `conditional`: no known violation, but missing or estimated evidence needs confirmation. `validation.passed` is false.
- `infeasible`: scoped offline-demo outcome when available fixtures cannot fit.
- `failure`: input/provider/model errors, exhausted planning limits or no checked plan found; it does not prove the entire request impossible.

Attached failed drafts remain explicitly unapproved. Unknown hours, dietary evidence and prices can yield conditional plans; explicit conflicts and known budget/time violations cannot be approved.

## Architecture

```text
Streamlit form / CLI JSON
        ↓
Pydantic preference normalization
        ↓
Nominatim city + optional neighborhood → shared cache / request gate
        ↓
Open-Meteo Saturday-window forecast → model context
        ↓
OpenAI native tool-calling loop
  search_places → Overpass / OSM catalog
  get_weather   → retrieved, cached forecast
  get_route     → Google Routes / labeled short-distance fallback
  estimate_cost → deterministic Decimal-based cost calculation
  validate_plan → original constraints + authoritative venue/route evidence
        ↓
submit_plan → independent server validation / bounded correction
        ↓
Checked alternative comparison → Streamlit itinerary, weather, map and JSON
```

| Module | Responsibility |
| --- | --- |
| `app.py`, `ui/controller.py` | Form mapping, progress callbacks, session state, option selection, safe export. |
| `ui/components.py`, `ui/map.py`, `ui/styles.py` | Itinerary, costs, weather, Folium map and presentation. |
| `agent/planner.py`, `agent/prompts.py`, `agent/schemas.py` | Bounded orchestration, tool registry, policy, typed input/output. |
| `providers/openai.py`, `providers/chat_completions.py`, `providers/llm.py` | Modular provider boundary and native multi-turn function-call protocol. |
| `providers/nominatim.py`, `providers/osm.py`, `providers/weather.py` | Geocoding, tag discovery/normalization and hourly forecast integration. |
| `providers/cache.py`, `providers/public_http.py` | Shared SQLite TTL cache, throttling, cooldowns and bounded safe HTTP calls. |
| `providers/google.py`, `providers/mock.py` | Google Routes and explicit Bengaluru fixtures. Legacy Places adapter is retained but inactive in the default planner. |
| `tools/` | Tool contracts, routing, costs, mandatory validation and conservative OSM hours parsing. |
| `main.py`, `tests/` | CLI entry point and offline/mock regression coverage. |

OpenAI decides which tools to invoke. Each assistant tool-call message remains in conversation history, and every executed function receives a matching `role=tool` response with its exact call ID. Results are returned before the next inference. Parallel calls are supported; malformed arguments and missing/duplicate IDs are rejected. `submit_plan` is a separate native function for structured final output, not a single itinerary-generation prompt.

Final validation runs independently against **original** preferences and discovered evidence. The model cannot override city, budget, travel mode or hard constraints through tool arguments. At least three distinct tools must execute before a recommendation is accepted. Limits remain 12 model turns by default, two server validation revisions and 24 tool executions. Model requests time out after 30 seconds with automatic retries disabled.

`Planner.add_alternatives` is a traced business-rule comparison after the genuine LLM run. It reuses discovered venues and prices, recalculates costs and validates every offered variant against the original constraints, sharing the tool cap. It is not presented as an extra model call. The fixture-based offline builder makes no LLM calls. The optional Groq adapter remains modular and inactive by default.

## Provider behavior, caching and operating policies

### OpenStreetMap / Overpass

Named restaurants/cafés (`amenity=restaurant|cafe`), museums/galleries/attractions (`tourism=museum|gallery|attraction`), parks/gardens (`leisure=park|garden`) and bookshops (`shop=books`) are queried with `nwr` around one compact anchor. Ways and relations use returned centers. Results retain real names, coordinates, available address/cuisine/opening-hour tags and explicit dietary/accessibility evidence. Private/no-access tags are excluded. Missing prices, ratings and amenities are never manufactured. Indoor/outdoor suitability is a disclosed venue-type heuristic, not proof of shelter or access.

Search results cache one hour; empty results cache five minutes. An empty search receives the existing single broader retry. Public-service errors back off and may use clearly labeled Bengaluru fixtures; other cities receive a scoped empty/error outcome. Area text does not trigger arbitrary LLM-generated geocoding or move the user-submitted anchor. No endpoint racing or automatic host rotation is used after throttling.

Attribution: [© OpenStreetMap contributors / ODbL](https://www.openstreetmap.org/copyright). Review [Overpass public-service guidance](https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances).

### Nominatim — read before live use or hosting

**Comply with the [public Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/).** This app uses only moderate-volume, user-triggered city/neighborhood submissions, with a descriptive User-Agent and project contact URL. Configure a maintained operator contact before hosting. There is no client-side autocomplete, bulk geocoding or generic address-search service. Do not submit confidential personal addresses.

Geocodes cache 30 days; misses cache one hour. All sessions and processes on **one host** must share the same `PROVIDER_CACHE_PATH`. SQLite serializes each entire Nominatim request and cache fill, maintaining at least **1.05 seconds** between starts and persisting rate-limit cooldowns. Multiple hosts need one centralized compliant geocoding proxy/service; separate cache files cannot enforce an application-wide rate limit. Do not clear the cache on every rerun. The endpoint is configurable without code changes.

Unresolved or implausibly distant neighborhoods fail gracefully instead of inventing an origin. A geocoded reference point cannot become an activity venue.

### Open-Meteo

One day forecast requests hourly precipitation probability, temperature and WMO weather codes with `timezone=auto`. Only hours overlapping the submitted window are used, including partial boundary hours. Responses cache 30 minutes. Missing values/hours and out-of-horizon dates remain partial/unavailable; they do not imply sunshine.

The model receives actual hourly evidence in context; `get_weather` reuses it without another HTTP request. Adverse precipitation (60%+), heavy weather codes or uncomfortable temperatures favor indoor discovery and alternatives. Validation warns when adverse weather overlaps outdoor stops. Unavailable forecasts retain an indoor-backup warning.

Attribution: [Open-Meteo.com](https://open-meteo.com/), CC BY 4.0. The [free API terms](https://open-meteo.com/en/terms) restrict use to non-commercial applications and fewer than 10,000 calls/day, 5,000/hour and 600/minute. Use a suitable service plan for commercial or larger deployments.

### Google Routes and approximate fallback

Walking/driving calls use OSM coordinates as `location.latLng`, a 12-second timeout, and only `routes.duration,routes.distanceMeters`. Repeated legs reuse evidence within the current planning run; Google responses are not written to the persistent provider cache. Way/relation centers may snap to an unsuitable road/entrance and require confirmation.

If Google is unavailable, short OSM/Nominatim journeys may use `source=distance_estimate`, `status=estimated`, `confidence=low`. Limits are 2 km straight-line for walking and 5 km for driving. Estimates use distance ×1.6, walking at 50 m/min +5 minutes, or driving at 166 m/min +10 minutes. These are planning allowances, **not navigable or Google-verified routes**. Mandatory validation counts their time and makes the result conditional. Longer or missing-coordinate legs remain unavailable. `ROUTING_FALLBACK=0` disables this fallback.

[Google Routes policies](https://developers.google.com/maps/documentation/routes/policies) require Google-derived map results to appear on a Google Map. Therefore Google durations/distances are shown in separate attributed route cards; the OSM map contains only OSM venue coordinates and schematic stop order. No Google geometry or tiles are drawn on it.

### Map and validation limits

Folium/Leaflet uses configurable HTTPS OSM-compatible tiles with visible attribution. Set `APP_PUBLIC_URL` to the actual origin before hosting. The iframe uses an explicit page base and non-restrictive referrer policy. The browser fetches tiles directly and uses normal HTTP caching. No bulk download, tile proxy, offline archive or background prefetch is implemented; the tile layer has no extra buffer and updates when idle. Follow the [OSM tile policy](https://operations.osmfoundation.org/policies/tiles/) and use an appropriate provider if traffic grows.

Costs use conservative upper bounds and Decimal arithmetic. Unknown items keep the complete total and remaining budget unknown. Venue prices cannot be called verified without evidence; driving requires a nonzero allowance. Chronology includes pre-activity buffers and every required route. OSM weekly hours support day lists/ranges, time ranges, overnight hours, `off` and `24/7`; complex holiday/seasonal/solar expressions remain unknown. Community tags and regular hours do not establish future availability.

Explicit dietary/accessibility conflicts are hard failures. Missing evidence is disclosed as conditional; unsupported requirements need manual confirmation. Interests, crowds and atmosphere are best effort. A shorter checked plan can omit optional interests, but cannot relax hard budget/time/dietary conflicts. Confirm current hours, menu, access, prices, weather and safe navigation before following conditional plans.

## CLI and tests

```sh
# Offline demo: no keys or network calls
python main.py --mock
python main.py --mock --input examples/input.json

# Real OpenAI with labeled fixture venues/routes
python main.py --mock-places --input examples/input.json

# Full live integrations
python main.py --input examples/input.json

# Regression tests: external HTTP responses are mocked
python -m pytest -q
```

Without activation on Windows, substitute `.\.venv\Scripts\python.exe` for `python`. The CLI also supports `--json '<JSON>'` or `--json -` for stdin. In PowerShell: `Get-Content -Raw examples/input.json | .\.venv\Scripts\python.exe main.py --mock --json -`. Exit code is 0 for approved/conditional results, 1 for failures/infeasibility.

In a restricted Windows environment, Streamlit AppTest may need local loopback access. If the default pytest temporary directory is inaccessible, choose a fresh project-local folder with `--basetemp=artifacts/pytest-local`; pytest owns and clears that chosen test directory.

**Latest verification: 199 tests passed.** Tests mock all external HTTP, including native multi-turn calls through the actual OpenAI SDK. They cover the assignment's Bengaluru example and Jaipur, normalization, node/way/relation centers, incomplete metadata, private access, rate limits/timeouts, concurrent geocoding/cache gates, weather selection, opening hours, original constraints, route fallbacks, tool IDs/history, alternatives, Streamlit rerenders, escaped marker text, attribution and credential redaction.

Separate live checks on **2026-10-09**:

| Integration | Actually tested live |
| --- | --- |
| Nominatim | Bengaluru, Jaipur and the Indiranagar starting neighborhood. |
| Overpass | Named places in Bengaluru and Jaipur. |
| Open-Meteo | Hourly forecasts for the requested Saturday in both cities. |
| Google Routes | Two walking routes and one driving route between retrieved OSM coordinates. |
| OpenAI | Full Bengaluru native tool-calling run: nine turns, conditional itinerary, three choices, zero known validation errors. |
| Folium/Leaflet | Browser rendering, OSM tiles, marker popups and visible attribution with the saved live result. |

Timeouts, throttling, incomplete metadata and rainy-weather decision paths were tested with mocked responses, not induced against live providers. Jaipur's provider data was checked live; its end-to-end LLM protocol was tested with mocks. Live smoke calls are separate from pytest and never run automatically. Incomplete prices/hours/access still require confirmation. Local evidence and caches under `artifacts/` are ignored by Git.

## AI tooling disclosure

Codex helped inspect and extend the existing backend/UI, implement provider adapters, and create/run failure-case tests.
Official provider documentation informed function calling, public-service limits and attribution.
OpenAI performs live planning; the offline demo deliberately uses no LLM.

## Submission checklist

- GitHub repository: [rishabh-soni/perfect-saturday-planner](https://github.com/rishabh-soni/perfect-saturday-planner).
- Local setup/run instructions, architecture, configuration, tests and AI-tool disclosure: this README.
- Hosted public URL: **pending**; the project has not been deployed.
- Optional demo video: not included.
