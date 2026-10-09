"""Provider-neutral agent boundary. Future providers implement this protocol."""
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel

from agent.schemas import Decision


@dataclass(frozen=True)
class ToolRequest:
    name: str
    arguments: dict
    call_id: str | None = None


@dataclass(frozen=True)
class ToolResult:
    request: ToolRequest
    output: dict


@dataclass(frozen=True)
class ModelTurn:
    calls: list[ToolRequest] = field(default_factory=list)
    decision: Decision | None = None
    correction: str | None = None
    diagnostics: dict = field(default_factory=dict)


class ModelSession(Protocol):
    def next_turn(self) -> ModelTurn: ...
    def respond(self, results: list[ToolResult]) -> None: ...
    def feedback(self, message: str) -> None: ...


class ModelProvider(Protocol):
    name: str
    def start(self, system_prompt: str, preferences: dict,
              tools: dict[str, type[BaseModel]], descriptions: dict[str, str]) -> ModelSession: ...


class ModelError(Exception):
    """Safe public message plus redacted provider diagnostics."""

    def __init__(self, message, *, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class ModelProtocolError(ModelError):
    pass
