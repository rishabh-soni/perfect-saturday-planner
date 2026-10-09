# Perfect Saturday Planner — Phase 1 backend

A Python 3.11+ Saturday planning agent using the OpenAI **Responses API** and native function calling. This phase provides a CLI and importable backend only; frontend and hosting are deferred.

## Run locally

```sh
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows PowerShell, instead:
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

No activation is necessary if you use `.venv\Scripts\python.exe` directly on Windows. Copy `.env.example` to `.env` and fill in keys **only for live mode**. `.env` is ignored by Git.

```sh
# Offline demonstration: no keys, no network, no LLM calls
python main.py --mock
python main.py --mock --input examples/input.json

# Real LLM selecting tools, with explicitly mocked Google data
python main.py --mock-places --input examples/input.json

# Real LLM, Google Places and Google Routes
python main.py --input examples/input.json

# Read arbitrary JSON from stdin
python main.py --mock --json - < examples/input.json

python -m pytest -q
```

For PowerShell stdin, use `Get-Content -Raw examples/input.json | python main.py --mock --json -`.

Environment variables:

| Variable | Use |
| --- | --- |
| `OPENAI_API_KEY` | Required for the actual LLM agent; absent keys produce a descriptive failure. |
| `OPENAI_MODEL` | Defaults to `gpt-4.1-mini`; choose a model supporting Responses function calling and structured outputs. |
| `GOOGLE_MAPS_API_KEY` | Optional; enable Places API (New) and Routes API, billing, and appropriate key restrictions. Missing/failed Places API uses labeled Bengaluru mock fixtures when available. |

Google APIs can incur charges. Places Text Search requests rating, regular hours, price level and vegetarian evidence through an explicit field mask. Routes requests duration and distance only. No keys are hardcoded, logged, or included in traces. HTTP error bodies are omitted.

## Input and output

The assignment sample is in `examples/input.json`, and is the default CLI input. Optional fields:

```json
{
  "starting_neighborhood": "central Bengaluru",
  "start_time": "10:00",
  "travel_mode": "walking",
  "saturday": "2026-10-10"
}
```

Duration supports `4 hours`, `90 minutes`, `1.5h`, and `2h 30m`. Saturday defaults to the upcoming Saturday using India time; an explicit date must be a Saturday. All schedules use city-local 24-hour time and must finish that day. All budgets/costs in this phase use INR; currency conversion is not supported.

JSON output includes normalized preferences, itinerary stops with provider-backed venue details, start/end times, activity duration, pre-activity buffer, itemized estimated costs, recommendation rationale, ordered routes, warnings/trade-offs, independent validation, and execution trace. Status values:

- `success`: all supported hard feasibility checks pass, while explicitly estimated prices remain disclosed.
- `conditional`: no known violation, but critical data such as hours, dietary evidence, unknown prices or mock routes require confirmation. `validation.passed` is **false**.
- `infeasible`: no viable result, or revision attempts could not resolve known violations.
- `failure`: invalid input, missing/unavailable model, refusal, malformed structured output, or iteration limit.

A draft attached to `failure`/`infeasible` is for transparency and must not be presented as an approved recommendation. Exit status is 0 for `success`/`conditional`, 1 otherwise. Without a starting neighborhood, the time window begins at the first venue; travel from home and the return journey are explicitly excluded. If an origin is supplied, it must be discovered and matched, and travel to the first stop must be accounted for.

The example offline plan uses nearby Cubbon Park and Koshy's, a short walk/rest and meal, and optional headphones while seated for music. Its fixtures are illustrative: **not current verified venue, price, hours, menu, crowd or route data**. It returns a conditional result with confirmation warnings rather than a false full validation pass.

## Architecture

```text
main.py                     CLI JSON boundary / dotenv loading
agent/schemas.py             Pydantic input, tool and final-output contracts
agent/prompts.py             Planning and evidence instructions
agent/planner.py             Responses tool loop, tool dispatch, trace, offline demo
tools/places.py              Venue discovery + explicit mock fallback
tools/routing.py             Discovered endpoint resolution + route availability
tools/budget.py              Decimal-based, deterministic INR cost calculation
tools/validator.py           Independent feasibility/evidence validation
providers/google.py         Google REST adapters with 12-second request timeout
providers/mock.py           Six curated Bengaluru fixtures + labeled route estimates
tests/                      Offline validation, agent protocol and provider tests
```

The real agent dynamically selects four native tools. Responses `function_call` items are executed in Python; each result is returned as a `function_call_output` using the matching **call_id**. All response output items are kept in the API conversation, including opaque reasoning items when applicable; reasoning is never exposed in the public trace. The SDK's Pydantic helper supplies strict tool schemas, and `responses.parse(text_format=Decision)` parses structured final output.

The server always validates the final proposal against **original preferences**, discovered venue IDs, and recorded route results, independently of model claims or model-initiated validation. The LLM cannot override the budget, city, mode or hard constraints through tool arguments. At least three distinct tools must execute before a recommendation is accepted. Stops reference discovered IDs; public venue names/addresses/hours are hydrated from the provider catalog.

Limits: six model iterations, two independent validation revisions, 24 total tool executions, 30-second model timeout with automatic retries disabled. Google requests also have bounded timeouts. Plans with known violations cannot be accepted. The offline mode uses real deterministic tool executions and a simple fixture-based plan builder; it is **not evidence of LLM planning** and is explicitly labeled `offline_demo`.

## Validation and failure handling

- Costs use upper range bounds for budget feasibility and Decimal arithmetic. Missing prices are `unknown`, never silently zero; totals and remaining budget are null if any price is unknown. Google price level never becomes a fabricated verified price. The current providers cannot substantiate verified activity prices, so those claims are rejected. Walking has a zero fare; driving needs an explicit allowance.
- Scheduling checks all activity intervals, route time, buffers, overlaps and the full elapsed window. No usable recorded route means rejection; live route failure never becomes a fabricated travel estimate. Mock routes use straight-line distance ×1.4 and fixed illustrative speeds, always labeled low confidence.
- Saturday regular opening periods are checked when present, including overnight and 24/7 periods. Unknown hours produce conditional output; regular hours do not guarantee holiday or future availability.
- Vegetarian/vegan/pure-vegetarian food evidence, wheelchair and alcohol-free requirements are hard constraints. Vegetarian options do not establish an exclusively vegetarian venue or vegan menu. Unknown evidence requires confirmation; explicit false evidence rejects the plan. Other hard constraints are conservatively marked as requiring manual confirmation.
- Crowds, quietness and low-energy preferences are soft. The server retries an empty search once with broader discovery in both agent and offline modes, records both actual searches in the trace, and retains all hard constraints for validation. The bounded loop prevents endless searches.
- Traces contain sanitized arguments, duration, success/failure, concise result summaries and revision events. Estimated results count as tool execution success; conditional/invalid validation does not count as a validation pass. No chain-of-thought is exposed.

## Tests and verification

Verified locally: **67 tests passed**. The sample CLI output was parsed back into the public Pydantic result schema and checked for all four tool actions. It produced a **105-minute**, **INR 650 upper-bound** mock plan with `status=conditional` and no known validation errors. Missing hours and mock evidence correctly prevent `validation.passed=true`.

Tests run with no external keys or network. They cover input normalization, hard/soft constraints, budget upper bounds, unknown costs, route/buffer accounting, venue evidence, missing/closed/overnight hours, Google authentication/rate limits/timeouts, routing failure, revisions, iteration limits, secret redaction and mock demo behavior. One test uses the **actual OpenAI Python SDK** with an HTTP mock transport to verify Responses request serialization, strict structured output parsing and matching function-call outputs. Live API integration requires supplied credentials and is not claimed as verified by these tests.

Official integration references: [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling), [structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs), [Google Text Search (New)](https://developers.google.com/maps/documentation/places/web-service/text-search), [Google Routes](https://developers.google.com/maps/documentation/routes/compute-route-over).

## Limitations and product trade-offs

Mock coverage is Bengaluru only. The offline builder is intentionally small, covers nearby 2–3-stop suggestions, and can shorten a plan to reduce time/cost; it is less flexible than the real agent. There is no live events feed, crowd data, verified menu/admission pricing, route safety check, reservations, currency conversion, conversational clarification flow, UI or deployment in this phase. Google data does not resolve arbitrary dietary/allergy/accessibility requirements; uncertainty is disclosed. Live API quotas/model access/billing must be configured by the operator.

`Planner` holds per-run evidence/trace state: create a new instance per request when integrating a concurrent web UI. It performs synchronous I/O and is not intended to be shared across simultaneous requests.

## AI tooling disclosure

Codex was used to implement the backend, create failure-case tests, and run the offline verification.
Official OpenAI and Google documentation informed the API adapters and Responses function-call protocol.
The real agent uses OpenAI for tool selection and personalized planning; the offline demonstration deliberately uses no LLM.
