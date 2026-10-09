"""Opt-in, bounded provider diagnostics; never persist prompts or hidden reasoning."""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


def enabled():
    return os.getenv("LLM_DIAGNOSTICS", os.getenv("OPENAI_DIAGNOSTICS", os.getenv("GROQ_DIAGNOSTICS", "0"))) == "1"


def redact(value):
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if re.search(r"key|secret|token$|authorization|password|thought|signature|reasoning", str(k), re.I)
                else redact(v) for k, v in list(value.items())[:60]}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value[:30]]
    if isinstance(value, str):
        value = re.sub(r"(?:AIza|sk-|gsk_)[A-Za-z0-9_-]+", "[REDACTED]", value)
        for name in ("GROQ_API_KEY", "GEMINI_API_KEY", "GOOGLE_MAPS_API_KEY", "OPENAI_API_KEY"):
            credential = os.getenv(name)
            if credential:
                value = value.replace(credential, "[REDACTED]")
        return value[:3000]
    return value


def record(data):
    safe = redact(data)
    if enabled():
        entry = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), **safe}
        path = Path(__file__).resolve().parents[1] / "artifacts" / "llm-diagnostics.jsonl"
        try:
            path.parent.mkdir(exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=True) + "\n")
        except OSError:
            # Diagnostics must not turn a usable response into a planner failure.
            pass
    return safe
