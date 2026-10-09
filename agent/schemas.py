"""Typed input, tool contracts and public output. Money is always INR."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def next_saturday() -> date:
    try:
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    except ZoneInfoNotFoundError:
        today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
    return today + timedelta(days=(5 - today.weekday()) % 7)


class Preferences(Schema):
    city: str = Field(min_length=1, max_length=120)
    budget: float = Field(ge=0, le=10_000_000)
    available_time: str = Field(min_length=1, max_length=80)
    mood: str = Field(min_length=1, max_length=500)
    interests: list[str] = Field(min_length=1, max_length=20)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    starting_neighborhood: str | None = None
    start_time: str = "10:00"
    travel_mode: Literal["walking", "driving"] = "walking"
    saturday: date = Field(default_factory=next_saturday)

    @field_validator("city", "mood", "available_time", "starting_neighborhood", mode="before")
    @classmethod
    def trim(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("Must not be blank")
        return value

    @field_validator("city")
    @classmethod
    def city_alias(cls, value):
        return "Bengaluru" if value.casefold() in {"bangalore", "bengaluru"} else value

    @field_validator("interests", "constraints")
    @classmethod
    def clean_list(cls, values):
        if any(not v.strip() or len(v) > 300 for v in values):
            raise ValueError("List entries must be nonblank and at most 300 characters")
        return list(dict.fromkeys(v.strip().lower() for v in values))

    @field_validator("start_time")
    @classmethod
    def valid_time(cls, value):
        clock_minutes(value)
        return value

    @model_validator(mode="after")
    def valid_window(self):
        if self.saturday.weekday() != 5:
            raise ValueError("saturday must be a Saturday")
        if self.duration_minutes <= 0 or clock_minutes(self.start_time) + self.duration_minutes > 1440:
            raise ValueError("Duration must be positive and finish within the same Saturday")
        return self

    @property
    def duration_minutes(self) -> int:
        # Full matching prevents silently accepting '4 hours tomorrow' or negative times.
        match = re.fullmatch(r"\s*(?:(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h))?\s*(?:(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|m))?\s*", self.available_time, re.I)
        if not match or not any(match.groups()):
            raise ValueError("available_time must be like '4 hours', '90 minutes' or '2h 30m'")
        return int(float(match[1] or 0) * 60 + float(match[2] or 0))

    @property
    def hard_constraints(self) -> list[str]:
        return [v for v in self.constraints if v not in self.soft_constraints]

    @property
    def soft_constraints(self) -> list[str]:
        return [v for v in self.constraints if "crowd" in v or v in {"quiet", "low energy", "relaxed"}]

    def context(self) -> dict:
        return {**self.model_dump(mode="json"), "duration_minutes": self.duration_minutes,
                "hard_constraints": self.hard_constraints, "soft_constraints": self.soft_constraints,
                "currency": "INR"}


def clock_minutes(value: str) -> int:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Time must be HH:MM in 24-hour local time")
    hours, minutes = map(int, value.split(":"))
    return hours * 60 + minutes


def clock_string(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


class Price(Schema):
    minimum: float | None
    maximum: float | None
    confidence: Literal["verified", "estimated", "unknown"]
    basis: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def valid_range(self):
        if self.confidence == "unknown":
            if self.minimum is not None or self.maximum is not None:
                raise ValueError("Unknown prices must have null bounds")
        elif self.minimum is None or self.maximum is None or not 0 <= self.minimum <= self.maximum:
            raise ValueError("Known prices require nonnegative, ordered bounds")
        return self


class Place(Schema):
    place_id: str
    name: str
    address: str
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    rating: float | None = Field(default=None, ge=0, le=5)
    opening_hours: dict | None = None
    price_level: str | None = None
    source: Literal["google_places", "mock"]
    types: list[str] = Field(default_factory=list)
    evidence: dict[str, bool] = Field(default_factory=dict)
    estimated_price: Price | None = None
    notes: list[str] = Field(default_factory=list)


class Waypoint(Schema):
    place_id: str | None
    latitude: float | None = Field(ge=-90, le=90)
    longitude: float | None = Field(ge=-180, le=180)

    @model_validator(mode="after")
    def usable(self):
        if not self.place_id and (self.latitude is None or self.longitude is None):
            raise ValueError("Provide place_id or both coordinates")
        return self


class Route(Schema):
    origin_id: str
    destination_id: str
    travel_mode: Literal["walking", "driving"]
    distance_meters: float | None = Field(ge=0)
    duration_minutes: float | None = Field(ge=0)
    status: Literal["available", "estimated", "unavailable"]
    source: Literal["google_routes", "mock", "unavailable"]
    confidence: Literal["provider_estimate", "low", "unavailable"]
    warning: str | None


class Stop(Schema):
    place_id: str
    activity: str = Field(min_length=1, max_length=500)
    kind: Literal["food", "walk", "music", "culture", "relax", "other"]
    start_time: str
    end_time: str
    buffer_minutes: int = Field(ge=0, le=120)
    cost: Price
    rationale: str = Field(min_length=1, max_length=1000)

    @field_validator("start_time", "end_time")
    @classmethod
    def valid_clock(cls, value):
        clock_minutes(value)
        return value


class Itinerary(Schema):
    title: str = Field(min_length=1, max_length=200)
    stops: list[Stop] = Field(min_length=1, max_length=5)
    starting_place_id: str | None
    transport_cost: Price
    warnings: list[str] = Field(max_length=30)
    trade_offs: list[str] = Field(max_length=20)


class Decision(Schema):
    status: Literal["recommendation", "infeasible"]
    message: str
    itinerary: Itinerary | None


class ValidationResult(Schema):
    passed: bool
    status: Literal["valid", "conditional", "invalid"]
    errors: list[str]
    warnings: list[str]
    total_minutes: float
    cost: dict


class TraceEvent(Schema):
    name: str
    arguments: dict
    duration_ms: float
    success: bool
    summary: str
    iteration: int


class PlannerResult(Schema):
    status: Literal["success", "conditional", "infeasible", "failure"]
    mode: Literal["agent", "offline_demo"]
    message: str
    preferences: dict | None = None
    itinerary: dict | None = None
    validation: ValidationResult | None = None
    trace: list[TraceEvent] = Field(default_factory=list)
    iterations: int = 0
    revisions: int = 0
