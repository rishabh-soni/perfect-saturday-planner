# Saturday, Sorted â€” Perfect Saturday Planner

A warm, responsive **Streamlit web app** powered by a Python 3.11+ Saturday planning agent using the **OpenAI** through the official `openai` Python SDK and native function calling. Phase 2 integrates the existing backend; hosting is deferred as requested.

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

Start the web application:

```sh
streamlit run app.py
# Windows without activating the environment:
.venv\Scripts\python.exe -m streamlit run app.py
```

Open the local URL printed by Streamlit. Choose **Try the Bengaluru demo** for an offline experience even when keys are configured. Without an OpenAI key, the UI automatically uses labeled demo data. To force a demo-only server for testing, set `SATURDAY_DEMO_ONLY=1` before starting Streamlit; unset it for the regular app. The checked-in appearance config never contains secrets.

The default form uses Bangalore, INR 2,000, four hours, **3 PM**, a tired-but-fun mood, food/music/walks and vegetarian/avoid-crowds preferences. The **The assignment example** preset switches to the original sample's 10 AM default. The CLI still accepts exactly the original JSON example.

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
| `OPENAI_MODEL` | Defaults to `gpt-4.1-mini`; override with an available OpenAI model supporting function calling. |
| `GOOGLE_MAPS_API_KEY` | Optional; enable Places API (New) and Routes API, billing, and appropriate key restrictions. Missing/failed Places API uses labeled Bengaluru mock fixtures when available. |
| `OPENAI_DIAGNOSTICS` | Optional: `1` enables redacted model details and local JSONL logs; default `0`. |
| `AGENT_MAX_ITERATIONS` | Model turn budget, default `12`, bounded to `3`–`20`. |
| `SATURDAY_DEMO_ONLY` | Optional UI setting: `1` forces an offline demo for local verification; default is personalized planning when an OpenAI key is available. |

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
- `infeasible`: scoped offline-demo outcome when the available fixtures cannot satisfy the request. Live model claims of infeasibility are challenged and, if unresolved, reported as search failures rather than proof of impossibility.
- `failure`: invalid input, missing/unavailable model, refusal, malformed output, exhausted planning limits, or no validated plan found among the searched options. This does not prove the user's request impossible.

A draft attached to `failure`/`infeasible` is for transparency and must not be presented as an approved recommendation. Exit status is 0 for `success`/`conditional`, 1 otherwise. Without a starting neighborhood, the time window begins at the first venue; travel from home and the return journey are explicitly excluded. If an origin is supplied, it must be discovered and matched, and travel to the first stop must be accounted for.

The example offline plan uses nearby Cubbon Park and Koshy's, a short walk/rest and meal, and optional headphones while seated for music. Its fixtures are illustrative: **not current verified venue, price, hours, menu, crowd or route data**. It returns a conditional result with confirmation warnings rather than a false full validation pass.

## Architecture

```text
main.py                     CLI JSON boundary / dotenv loading
app.py                      Streamlit entry point, form, progress, session state
ui/controller.py            Input mapping, safe agent boundary, download redaction
ui/components.py            Itinerary, route, budget and real trace presentation
ui/styles.py                Lightweight warm theme and responsive CSS
.streamlit/config.toml      Appearance and local server defaults (no secrets)
agent/schemas.py             Pydantic input, tool and final-output contracts
agent/prompts.py             Planning and evidence instructions
agent/planner.py             Provider-neutral tool loop, tool dispatch, trace, offline demo
tools/places.py              Venue discovery + explicit mock fallback
tools/routing.py             Discovered endpoint resolution + route availability
tools/budget.py              Decimal-based, deterministic INR cost calculation
tools/validator.py           Independent feasibility/evidence validation
providers/llm.py            Model/session protocols for provider-neutral model adapters
providers/chat_completions.py Shared native Chat Completions tool protocol
providers/openai.py         Active OpenAI endpoint, model and error mapping
providers/groq.py           Retained optional Groq adapter, inactive by default
providers/google.py         Google REST adapters with 12-second request timeout
providers/mock.py           Six curated Bengaluru fixtures + labeled route estimates
tests/                      Offline validation, agent protocol and provider tests
```

The real agent dynamically selects `search_places`, `get_route`, `estimate_cost`, and `validate_plan` using OpenAI Chat Completions. `OpenAIModel` creates the official `OpenAI` client with an explicit `OPENAI_API_KEY` and `base_url="https://api.openai.com/v1"`. The default model is `gpt-4.1-mini`, overridable through `OPENAI_MODEL`. Old Groq/Gemini keys and endpoint environment variables do not change the active provider.

OpenAI and the retained optional Groq adapter share `providers/chat_completions.py`, so they use the same multi-turn protocol and independent server checks. Only provider configuration and safe error mapping differ.

Tool schemas come from the existing Pydantic models. Each assistant message containing `tool_calls` is retained in conversation history, then Python executes the requested tools and appends one `role="tool"` response with the exact `tool_call_id` and function name for every call. All results are sent back to the active provider before another inference step. Parallel calls are supported; missing or duplicate IDs and mismatched responses are rejected. Malformed JSON arguments receive matching error responses and a bounded correction attempt rather than executing invalid data.

A fifth native function, `submit_plan`, supplies the complete `Decision` schema for final structured output. Pydantic checks the submission before independent server validation. Invalid schemas and rejected drafts receive matching tool responses requesting correction; submissions alongside planning tools are deferred until their results are reviewed. Text-only answers are not accepted as itineraries. SDK reasoning extras are excluded from logs and request history.

`Planner(llm=provider)` accepts the provider-neutral `ModelProvider`/`ModelSession` protocols, allowing future adapters without changing tools, validation or frontend. `Planner(client=client)` injects an OpenAI SDK client configured for OpenAI, useful for deterministic HTTP transport tests.

The server always validates the final proposal against **original preferences**, discovered venue IDs, and recorded route results, independently of model claims or model-initiated validation. The LLM cannot override the budget, city, mode or hard constraints through tool arguments. At least three distinct tools must execute before a recommendation is accepted. Stops reference discovered IDs; public venue names/addresses/hours are hydrated from the provider catalog.

Limits: 12 model iterations by default (`AGENT_MAX_ITERATIONS`, clamped to 3–20; invalid values use 12), two independent validation revisions, 24 total tool executions, 30-second model timeout with automatic retries disabled. Google requests also have bounded timeouts. Plans with known violations cannot be accepted. The offline mode uses real deterministic tool executions and a simple fixture-based plan builder; it is **not evidence of LLM planning** and is explicitly labeled `offline_demo`.

## Frontend behavior

The form maps interests, optional neighborhood, INR budget, duration, mood, constraints and travel mode into the existing `Preferences` schema. CafÃ© interests normalize to food and history to culture; avoid crowds normalizes to the sample constraint. Budget-friendly uses the numeric budget check. Low walking remains a hard requirement, with an explicit confirmation warning when the existing backend cannot substantiate it; it is never silently relaxed.

An optional `Planner(on_event=callback)` receives a sanitized copy of each **actual recorded event**. The UI updates a native Streamlit status container as those events occur. No fabricated tool actions, reasoning or artificial sleeps are used. The complete trace is retained under **How your Saturday was planned**, including after rerenders. A fresh planner is created for every request.

Results and last submitted preferences persist in `st.session_state`. **Plan again** reuses the last submitted preferences and planning mode; submit the form to apply edits. Input validation errors preserve any previous plan while clearly explaining that a new plan was not generated. Unexpected backend exceptions produce a helpful failure message and retain any actual events recorded before the interruption.

The itinerary shows provider-backed venue data, activity rationale, timing, cost confidence and ordered travel segments. Maps uses Google place IDs when available; demo venues use name/address searches rather than pretending approximate coordinates are verified. Unknown costs stay unknown and are omitted from the native spending chart. Failure/infeasible drafts are shown only in a clearly marked unapproved-draft expander, never as an accepted itinerary. Downloaded JSON and all displayed fields are redacted; the complete trace is preserved.

## Validation and failure handling

- Costs use upper range bounds for budget feasibility and Decimal arithmetic. Missing prices are `unknown`, never silently zero; totals and remaining budget are null if any price is unknown. Google price level never becomes a fabricated verified price. The current providers cannot substantiate verified activity prices, so those claims are rejected. Walking has a zero fare; driving needs an explicit allowance.
- Scheduling checks all activity intervals, route time, buffers, overlaps and the full elapsed window. No usable recorded route means rejection; live route failure never becomes a fabricated travel estimate. Mock routes use straight-line distance Ã—1.4 and fixed illustrative speeds, always labeled low confidence.
- Saturday regular opening periods are checked when present, including overnight and 24/7 periods. Unknown hours produce conditional output; regular hours do not guarantee holiday or future availability.
- Vegetarian/vegan/pure-vegetarian food evidence, wheelchair and alcohol-free requirements are hard constraints. Vegetarian options do not establish an exclusively vegetarian venue or vegan menu. Unknown evidence requires confirmation; explicit false evidence rejects the plan. Other hard constraints are conservatively marked as requiring manual confirmation.
- Crowds, quietness and low-energy preferences are soft. The server retries an empty search once with broader discovery in both agent and offline modes, records both actual searches in the trace, and retains all hard constraints for validation. The bounded loop prevents endless searches.
- Traces contain sanitized arguments, duration, success/failure, concise result summaries and revision events. Estimated results count as tool execution success; conditional/invalid validation does not count as a validation pass. No chain-of-thought is exposed.

## Model diagnostics

Set `OPENAI_DIAGNOSTICS=1` to capture redacted upstream error messages and function-call arguments. Basic HTTP status, model, run ID, turn, timing, response/request IDs, finish reason and usage metadata are available in model trace events. Invalid submissions include exact schema error fields/types/messages, also returned to the active provider for correction. Raw reasoning, response bodies, `failed_generation`, prompts and headers are excluded.

In **How your Saturday was planned**, select **Show sanitized tool arguments and model diagnostics**. The downloaded result also includes these trace diagnostics. With detailed diagnostics enabled, the server appends JSON lines to `artifacts/llm-diagnostics.jsonl`, ignored by Git. Each entry has a UTC timestamp, run ID and turn so concurrent requests can be distinguished. These logs can contain user preferences or venue details inside function arguments; keep them local and disable detailed logging with `OPENAI_DIAGNOSTICS=0` when finished. Credentials are redacted; full prompts, HTTP headers and hidden reasoning/signatures are never logged. Diagnostic file failures do not interrupt planning.

## Tests and verification

Verified locally: **151 tests passed**, including existing backend and frontend/integration tests and actionable OpenAI/Groq model-error tests. The sample CLI output was parsed back into the public Pydantic result schema and checked for all four tool actions. It produced a **105-minute**, **INR 650 upper-bound** mock plan with `status=conditional` and no known validation errors. Missing hours and mock evidence correctly prevent `validation.passed=true`.

Streamlit AppTest verifies startup, form mapping, submission into the actual mock backend, rerender persistence, regeneration, invalid input, unknown-city failure, and rejected draft presentation. Other tests verify callback/trace equality, Maps URL provenance, unknown-cost categories, safe HTML escaping and secret-free downloads. A local Streamlit server returned **HTTP 200** on `/_stcore/health`; the browser demo was generated and inspected at mobile and desktop breakpoints. These tests make no external API calls. In restricted Windows sandboxes, Streamlit AppTest needs permission for local loopback sockets even though it does not contact external services.

Tests run with no external keys or network. They cover input normalization, hard/soft constraints, budget upper bounds, unknown costs, route/buffer accounting, venue evidence, missing/closed/overnight hours, Google authentication/rate limits/timeouts, routing failure, revisions, iteration limits, secret redaction and mock demo behavior. Tests use the **actual OpenAI Python SDK** with an HTTP mock transport against both OpenAI and Groq URLs to verify tool schemas, multi-turn history, parallel calls, every planning tool, matching call IDs, final submissions, correction feedback, malformed JSON and safe API errors. The new OpenAI key is configured only in the ignored local `.env`; Groq credentials were removed from active local configuration. The latest live run used `gpt-4.1-mini`, real Places/Routes data, Bengaluru, INR 2,000, four hours starting at 15:00, driving, vegetarian and avoid-crowds preferences. A larger LLM draft failed validation; bounded recovery produced a one-stop provisional plan from 15:10–16:10 at a discovered venue, with a INR 600 upper estimate, zero validation errors and five confirmation warnings. It completed on turn 11 with one revision. Separate music/walk stops were explicitly omitted. This is a conditional plan, not a guarantee of prices, dietary availability or crowd conditions.

Official integration references: [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling), [GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini), [Groq local tool calling](https://console.groq.com/docs/tool-use/local-tool-calling), [Groq OpenAI compatibility](https://console.groq.com/docs/openai), [Groq structured outputs](https://console.groq.com/docs/structured-outputs), [OpenAI Python SDK](https://developers.openai.com/api/reference/python), [Google Text Search (New)](https://developers.google.com/maps/documentation/places/web-service/text-search), [Google Routes](https://developers.google.com/maps/documentation/routes/compute-route-over).

## Limitations and product trade-offs

Mock coverage is Bengaluru only. The offline builder is intentionally small, covers nearby 2â€“3-stop suggestions, and can shorten a plan to reduce time/cost; it is less flexible than the real agent. There is no live events feed, crowd data, verified menu/admission pricing, route safety check, reservations, currency conversion, conversational clarification flow or deployment in this phase. Google data does not resolve arbitrary dietary/allergy/accessibility requirements; uncertainty is disclosed. OpenAI quota/rate limits (429), key permissions (401/403), unavailable models (404), rejected requests (400), server errors and connectivity failures return safe actionable messages with the actual trace retained. Raw upstream bodies and keys are omitted; exhausted quota does not trigger repeated paid requests. The demo remains available.

Configure `OPENAI_API_KEY` in the ignored local `.env` or hosting secrets. `GOOGLE_MAPS_API_KEY` remains separately configured for Places and Routes. Without an OpenAI key, the UI explicitly uses the labeled offline demo; a direct live CLI request returns a missing-key failure. No secret is included in tracked configuration. The optional Groq adapter can still be injected with `Planner(llm=GroqModel(...))`; it is not selected automatically.

`Planner` holds per-run evidence/trace state: the UI creates a new instance per request. It performs synchronous I/O and is not shared across simultaneous requests. Session state persists across Streamlit reruns within a browser session; a full page refresh/new session may clear it.

## Deployment preparation (not deployed)

Use `app.py` as the Streamlit entry point and install `requirements.txt` on Python 3.11+. Configure `OPENAI_API_KEY`, `GOOGLE_MAPS_API_KEY` and `OPENAI_MODEL` as **server-side hosting secrets** or environment variables. The app also reads Streamlit secrets (locally `.streamlit/secrets.toml`, which is ignored). Never upload `.env` or a real secrets file to GitHub. Keep `SATURDAY_DEMO_ONLY` unset for live planning. Verify Places API (New) and Routes API access with the backend Google key; an Android-restricted key is unsuitable for these server requests. No hosting resources, deploys or public URLs were created in Phase 2.

Streamlit references used for implementation: [status containers](https://docs.streamlit.io/develop/api-reference/status/st.status), [forms](https://docs.streamlit.io/develop/api-reference/execution-flow/st.form), [session state](https://docs.streamlit.io/develop/api-reference/caching-and-state/st.session_state), and [AppTest](https://docs.streamlit.io/develop/api-reference/app-testing/st.testing.v1.apptest).

## AI tooling disclosure

Codex was used to implement the backend, create failure-case tests, and run the offline verification.
Official OpenAI and Groq documentation informed the provider adapters and native function-call protocol.
The real agent uses OpenAI for tool selection and personalized planning; the offline demonstration deliberately uses no LLM.

The agent now reserves time for final submission, batches independent calls, prefers compact venue clusters and reuses existing evidence. With three model turns remaining it receives a traced reminder to finish or return infeasible. The 24-tool execution cap and two validation revisions remain enforced; increasing the turn budget never bypasses feasibility checks.

The OpenAI-compatible session reserves the final two turns by offering only `submit_plan` with forced native function choice. The server still validates that submission, and the last turn can correct a rejected draft. This prevents a final tool validation from consuming every turn without returning a Decision. No invalid plan is auto-approved at the turn limit.

OpenAI tool declarations use the SDK's strict Pydantic schemas, including `submit_plan`, so mandatory Price objects cannot be emitted as null. Groq retains its compatible non-strict declarations. Python tool validation still runs independently and returns safe field/type/message hints without raw input, allowing bounded correction of semantic errors.

Live infeasibility handling now challenges the first unsupported model claim once, asking for a simpler one- or two-stop plan under the original hard constraints. Interests and crowd preferences are best effort; estimated costs and unknown evidence are disclosed rather than treated as automatic infeasibility. Continued inability to find a validated proposal returns `failure` with a scoped explanation. Repeated invalid drafts also return search failure after the existing two-revision bound, and remain unapproved.

The supplied start time is arrival/window start, not an exact first-activity time. A 15:00 arrival with a ten-minute buffer permits a 15:10 activity, while the four-hour deadline remains 19:00. Scheduling errors now include the earliest permitted activity start so the model can make a concrete correction. Regression tests cover this afternoon driving scenario, unknown prices, crowd warnings and recovery from premature infeasibility.

A bounded recovery can shorten an LLM draft to one discovered venue when route evidence or scheduling prevents the larger plan. It preserves the original activity duration and cost, applies the first buffer within the original deadline, retains any supplied origin and required recorded route, recalculates the cost, and revalidates all hard checks. The trace labels this `simplify_plan`; it is not presented as a fabricated model tool call. Recovery consumes the existing two-revision and 24-tool budgets. It cannot bypass explicit dietary conflicts, the budget, opening hours, an unresolved starting neighborhood or missing required route evidence. Omitted interests and uncertain crowd/music evidence are disclosed.


## Choosing between plans

The UI defaults to an 11:00 start, four hours, INR 2,000, walking, food and walks, with vegetarian and crowd preferences. The assignment example remains a separate preset. Defaults favour a simple daytime outing; live hours and prices still need confirmation.

After the native LLM tool-calling run, `Planner.add_alternatives` compares up to three distinct options using discovered venues and existing prices. Shorter options explain omitted interests, retain the original hard constraints, recalculate costs and pass the same independent validator. Known Saturday hours can shift a stop later inside the original window. Comparison is a traced business-rule phase (`compare_options`), not an extra LLM call. It shares the 24-tool cap. If fewer options can be supported, the UI says so rather than fabricating choices. API failures without usable evidence remain failures. The offline demo supports the same choice UI with labelled fixtures.

Selecting an option updates its itinerary, budget, warnings and downloaded JSON. The export retains all choices and the real trace. Budget clarity uses an itemized range-and-basis table. Unknown costs keep the complete total and remaining budget unknown; the known subtotal is shown separately.
