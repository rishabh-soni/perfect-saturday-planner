"""Shared OpenAI-compatible native Chat Completions tool protocol."""
from __future__ import annotations

import json
from time import perf_counter
from uuid import uuid4

from openai import OpenAI, APIStatusError, APITimeoutError, APIConnectionError, pydantic_function_tool
from pydantic import ValidationError

from agent.schemas import Decision
from providers.diagnostics import enabled, record
from providers.llm import ModelError, ModelProtocolError, ModelTurn, ToolRequest

FINAL_FUNCTION = "submit_plan"


def declarations(tool_models, descriptions, *, strict=False):
    models = {**tool_models, FINAL_FUNCTION: Decision}
    if strict:
        return [pydantic_function_tool(model, name=name, description=descriptions.get(name,
            "Submit the final Decision alone after reviewing tool results. Server validation may request a revision."))
                for name, model in models.items()]
    return [{"type": "function", "function": {
        "name": name,
        "description": descriptions.get(name, "Submit the final Decision alone after reviewing tool results. Server validation may request a revision."),
        "parameters": model.model_json_schema(),
    }} for name, model in models.items()]


class ChatModel:

    def __init__(self, *, api_key, model, base_url, name, error_message, client=None):
        self.name, self.error_message = name, error_message
        self.strict_tools = False
        self.model = model
        self._owned_client = client is None
        self.client = client if client is not None else OpenAI(
            api_key=api_key, base_url=base_url, timeout=30, max_retries=0)

    def start(self, system_prompt, preferences, tools, descriptions):
        return ChatSession(self.client, self.model, system_prompt, preferences, declarations(tools, descriptions, strict=self.strict_tools), self.name, self.error_message)

    def close(self):
        if self._owned_client:
            self.client.close()


class ChatSession:
    def __init__(self, client, model, system_prompt, preferences, tools, provider, error_message):
        self.provider, self.error_message = provider, error_message
        self.client, self.model, self.tools = client, model, tools
        self.history = [{"role": "system", "content": system_prompt +
            "\nUse native function calls to plan. For the final Decision call submit_plan alone. "
            "Review tool results before submitting; correct rejected submissions using their tool responses."},
            {"role": "user", "content": json.dumps(preferences, allow_nan=False)}]
        self.pending = []
        self.finalizing = False
        self.run_id, self.turn = uuid4().hex, 0

    def next_turn(self):
        if self.pending:
            raise ModelProtocolError("Pending model calls must receive tool responses first.")
        self.turn += 1
        started = perf_counter()
        diagnostic = {"provider": self.provider, "model": self.model, "run_id": self.run_id,
                      "turn": self.turn, "history_messages": len(self.history)}
        try:
            response = self.client.chat.completions.create(
                model=self.model, messages=self.history,
                tools=[t for t in self.tools if t["function"]["name"] == FINAL_FUNCTION] if self.finalizing else self.tools,
                tool_choice={"type": "function", "function": {"name": FINAL_FUNCTION}} if self.finalizing else "required",
                parallel_tool_calls=not self.finalizing,
                max_completion_tokens=4500, temperature=0.4)
        except APIStatusError as error:
            body = error.body if isinstance(error.body, dict) else {}
            body = body.get("error", body)
            body = body if isinstance(body, dict) else {}
            diagnostic.update(http_status=error.status_code, api_status=body.get("code") or body.get("type"),
                              request_id=error.request_id, duration_ms=round((perf_counter()-started)*1000, 3))
            if enabled():
                diagnostic["upstream_message"] = body.get("message")
            # failed_generation may contain reasoning; never persist raw bodies.
            raise ModelError(self.error_message(error), diagnostics=record(diagnostic)) from None
        except APITimeoutError:
            raise ModelError(f"{self.provider} timed out. Retry later or use the Bengaluru demo.",
                             diagnostics=record({**diagnostic, "error_type": "timeout"})) from None
        except APIConnectionError:
            raise ModelError(f"Could not connect to {self.provider}. Check connectivity or use the Bengaluru demo.",
                             diagnostics=record({**diagnostic, "error_type": "connection"})) from None
        diagnostic.update(duration_ms=round((perf_counter()-started)*1000, 3), response_id=response.id,
                          finish_reasons=[c.finish_reason for c in response.choices])
        if response.usage:
            diagnostic["usage"] = response.usage.model_dump(mode="json", exclude_none=True)
        if not response.choices:
            raise ModelProtocolError(f"{self.provider} returned no usable candidate. Retry or use the demo.", diagnostics=record(diagnostic))
        message = response.choices[0].message
        calls = message.tool_calls or []
        if enabled():
            diagnostic["function_calls"] = [{"name": c.function.name, "id": c.id,
                "arguments": c.function.arguments} for c in calls if c.type == "function"]
        diagnostic = record(diagnostic)
        if not calls:
            # Do not accept untyped prose or expose a reasoning model's raw text.
            self.history.append({"role": "assistant", "content": message.content or ""})
            return ModelTurn(correction="No native function call was returned. Use planning tools or submit_plan with the complete Decision schema.", diagnostics=diagnostic)
        if any(c.type != "function" or not c.id for c in calls) or len({c.id for c in calls}) != len(calls):
            raise ModelProtocolError(f"{self.provider} returned unsupported calls or missing/duplicate call IDs.", diagnostics=diagnostic)
        # Only standard chat fields go back to the provider; reasoning extras remain private.
        self.history.append({"role": "assistant", "content": message.content,
                             "tool_calls": [c.model_dump(include={"id", "type", "function"}) for c in calls]})
        self.pending = calls
        requests = []
        malformed = []
        for call in calls:
            try:
                args = json.loads(call.function.arguments)
                if not isinstance(args, dict):
                    raise ValueError("Function arguments must be an object")
            except (ValueError, TypeError):
                malformed.append(call.id)
                continue
            requests.append(ToolRequest(call.function.name, args, call.id))
        if malformed:
            return ModelTurn(correction="Function arguments must be valid JSON objects. Correct the calls and retry.",
                             diagnostics=record({**diagnostic, "malformed_call_ids": malformed}))
        planning = [r for r in requests if r.name != FINAL_FUNCTION]
        if planning:
            return ModelTurn(calls=planning, diagnostics=diagnostic)
        if len(requests) != 1:
            return ModelTurn(correction="Call submit_plan exactly once and alone.", diagnostics=diagnostic)
        try:
            return ModelTurn(decision=Decision.model_validate(requests[0].arguments), diagnostics=diagnostic)
        except ValidationError as error:
            issues = [{"field": ".".join(map(str, e["loc"])), "type": e["type"], "message": e["msg"]}
                      for e in error.errors(include_input=False, include_context=False, include_url=False)]
            return ModelTurn(correction="Correct the submit_plan schema fields: " + json.dumps(issues),
                             diagnostics=record({**diagnostic, "schema_errors": issues}))

    def _respond(self, outputs):
        for call, output in zip(self.pending, outputs):
            self.history.append({"role": "tool", "tool_call_id": call.id, "name": call.function.name,
                                 "content": json.dumps(output, allow_nan=False)})
        self.pending = []

    def respond(self, results):
        expected = [c for c in self.pending if c.function.name != FINAL_FUNCTION]
        if len(expected) != len(results) or any(c.id != r.request.call_id or c.function.name != r.request.name
                                               for c, r in zip(expected, results)):
            raise ModelProtocolError("Tool responses did not match the model's pending call IDs and names.")
        iterator = iter(results)
        self._respond([{"error": "Submission deferred: review planning tool results, then submit_plan alone."}
                       if c.function.name == FINAL_FUNCTION else {"result": next(iterator).output}
                       for c in self.pending])

    def finalize(self):
        if self.pending:
            raise ModelProtocolError("Pending calls must receive responses before final submission.")
        self.finalizing = True
        self.feedback("Now submit_plan using the available venue and route evidence. Reuse the last valid itinerary if available. Shorten the plan to satisfy time, route and budget checks; do not invent missing evidence. Interests and crowds are soft preferences. Prefer a one- or two-stop provisional recommendation with estimated costs and warnings. Unknown prices/hours do not automatically make a plan infeasible. Schedule the first activity after its buffer within the original time window. Only if even that shorter plan cannot pass known hard checks, submit infeasible with specific blockers among the searched candidates; do not claim the entire request is impossible. Server validation may ask you to correct your submission.")

    def feedback(self, message):
        if self.pending:
            self._respond([{"error": message} for _ in self.pending])
        else:
            self.history.append({"role": "user", "content": message})
