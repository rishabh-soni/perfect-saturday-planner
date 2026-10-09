"""GroqCloud configuration for the shared native function-calling adapter."""
from providers.chat_completions import ChatModel, declarations

BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-20b"


def api_error_message(error):
    code = getattr(error, "status_code", None)
    if code == 429:
        return "Groq API quota is exhausted or this request was rate-limited. Check GroqCloud usage limits and retry later, or use the Bengaluru demo."
    if code in (401, 403):
        return "Groq rejected the API key or denied access. Check GROQ_API_KEY and project permissions, or use the demo."
    if code == 404:
        return "The configured Groq model is unavailable. Check GROQ_MODEL and model access, or use the demo."
    if code == 400:
        return "Groq rejected the request or generated an invalid tool call. Inspect sanitized model diagnostics or use the demo."
    if code == 504:
        return "Groq exceeded its response deadline (HTTP 504). Retry later or use the Bengaluru demo."
    if isinstance(code, int) and code >= 500:
        return "Groq is temporarily unavailable. Retry later or use the Bengaluru demo."
    return "Groq could not complete this request. Retry later or use the Bengaluru demo."


class GroqModel(ChatModel):
    def __init__(self, *, api_key=None, model=DEFAULT_MODEL, client=None):
        super().__init__(api_key=api_key, model=model, base_url=BASE_URL,
                         name="Groq", error_message=api_error_message, client=client)
