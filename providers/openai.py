"""OpenAI configuration for the shared native function-calling adapter."""
from providers.chat_completions import ChatModel, declarations

BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4.1-mini"


def api_error_message(error):
    status = getattr(error, "status_code", None)
    body = getattr(error, "body", None)
    body = body.get("error", body) if isinstance(body, dict) else {}
    code = body.get("code") if isinstance(body, dict) else None
    if status == 429:
        if code in {"insufficient_quota", "credit_balance_exhausted", "billing_hard_limit_reached"}:
            return "OpenAI API credits or quota are exhausted. Check project billing and usage limits, or use the Bengaluru demo."
        return "OpenAI rate-limited this request. Retry later or use the Bengaluru demo."
    if status in (401, 403):
        return "OpenAI rejected the API key or denied access. Check OPENAI_API_KEY and project permissions, or use the demo."
    if status == 404:
        return "The configured OpenAI model is unavailable. Check OPENAI_MODEL and model access, or use the demo."
    if status == 400:
        return "OpenAI rejected the request. Inspect sanitized model diagnostics or use the demo."
    if status == 504:
        return "OpenAI exceeded its response deadline (HTTP 504). Retry later or use the Bengaluru demo."
    if isinstance(status, int) and status >= 500:
        return "OpenAI is temporarily unavailable. Retry later or use the Bengaluru demo."
    return "OpenAI could not complete this request. Retry later or use the Bengaluru demo."


class OpenAIModel(ChatModel):
    def __init__(self, *, api_key=None, model=DEFAULT_MODEL, client=None):
        super().__init__(api_key=api_key, model=model, base_url=BASE_URL,
                         name="OpenAI", error_message=api_error_message, client=client)
        self.strict_tools = True
