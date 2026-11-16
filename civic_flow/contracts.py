"""接口请求与领域错误的数据契约。"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from enum import Enum
from pydantic import BaseModel, Field, field_validator, model_validator

_CLOCK_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, detail: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.detail = code, message, status, detail or {}

def utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise DomainError("NAIVE_TIME", "Timestamp requires UTC offset")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")

def parse_time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise DomainError("BAD_TIME", "Timestamp must be ISO 8601") from None
    if result.tzinfo is None:
        raise DomainError("NAIVE_TIME", "Timestamp requires UTC offset")
    return result.astimezone(timezone.utc)

class EventKind(str, Enum):
    ASSIGN = "assigned"
    RESPONSE = "responded"
    EXPLAIN = "explained"
    ACK = "acknowledged"
    CLOSE = "closed"
    REOPEN = "reopened"

class Resolution(str, Enum):
    SOLVED = "solved"
    EXPLAINED = "explained"
    OUT_OF_SCOPE = "out_of_scope"

class CommitmentKind(str, Enum):
    NATURAL = "natural"
    WORKING = "working"

class AwareModel(BaseModel):
    @field_validator("occurred_at", "submitted_at", "as_of", "effective_from",
                     check_fields=False)
    @classmethod
    def aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return value
        if value.tzinfo is None:
            raise ValueError("Timestamp requires UTC offset")
        return value.astimezone(timezone.utc)

class CaseInput(AwareModel):
    external_id: str = Field(min_length=1, max_length=100)
    grid_id: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=5, max_length=400)
    occurred_at: datetime
    source: str = Field(default="resident", min_length=1, max_length=40)
    priority: int = Field(default=2, ge=1, le=3)
    resident_token: str = Field(min_length=4, max_length=100)

    @field_validator("external_id", "grid_id", "category", "source", "resident_token")
    @classmethod
    def clean_key(cls, value: str) -> str:
        if value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Identifier must be trimmed and printable")
        return value

class BatchInput(AwareModel):
    batch_key: str = Field(min_length=1, max_length=100)
    cases: list[CaseInput] = Field(min_length=1, max_length=500)
    submitted_at: datetime

class GridInput(BaseModel):
    grid_id: str = Field(min_length=1, max_length=80)
    district: str = Field(min_length=1, max_length=80)
    active: bool = True

class RouteRuleInput(AwareModel):
    category: str = Field(min_length=1, max_length=80)
    department: str = Field(min_length=1, max_length=80)
    priority: int = Field(default=100, ge=0, le=1000)
    response_hours: int = Field(default=72, ge=1, le=720)
    resolution_hours: int = Field(default=336, ge=1, le=2160)
    response_kind: CommitmentKind = CommitmentKind.NATURAL
    resolution_kind: CommitmentKind = CommitmentKind.NATURAL
    calendar_id: str | None = Field(default=None, max_length=80)
    effective_from: datetime | None = None

    @model_validator(mode="after")
    def deadlines(self):
        if self.resolution_hours < self.response_hours:
            raise ValueError("Resolution deadline precedes response deadline")
        if CommitmentKind.WORKING in (self.response_kind, self.resolution_kind):
            if not self.calendar_id or not self.calendar_id.strip():
                raise ValueError("Working-time commitment requires calendar_id")
        return self

WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

class CalendarInput(AwareModel):
    calendar_id: str = Field(min_length=1, max_length=80)
    effective_from: datetime | None = None
    utc_offset: str = Field(default="+00:00", pattern=r"^[+-]?\d{2}:\d{2}$")
    workweek: dict[str, list[tuple[str, str]]]
    holidays: list[str] = Field(default_factory=list, max_length=400)

    @field_validator("calendar_id")
    @classmethod
    def clean_calendar_id(cls, value: str) -> str:
        if value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Identifier must be trimmed and printable")
        return value

    @field_validator("workweek")
    @classmethod
    def windows_shape(cls, value: dict) -> dict:
        if not value:
            raise ValueError("Workweek needs at least one day")
        for day, windows in value.items():
            if day not in WEEKDAY_NAMES:
                raise ValueError("Unknown weekday: " + str(day))
            if not windows:
                raise ValueError("Day needs at least one window")
            for window in windows:
                if len(window) != 2:
                    raise ValueError("Window must be [start, end]")
                for edge in window:
                    if not isinstance(edge, str) or not _CLOCK_RE.match(edge):
                        raise ValueError("Window edge must be HH:MM")
                if window[0] >= window[1]:
                    raise ValueError("Window start must precede end")
        return value

    @field_validator("holidays")
    @classmethod
    def holiday_shape(cls, value: list[str]) -> list[str]:
        for item in value:
            try:
                date.fromisoformat(item)
            except ValueError:
                raise ValueError("Holiday must be YYYY-MM-DD") from None
            if len(item) != 10:
                raise ValueError("Holiday must be YYYY-MM-DD")
        return value

class EventInput(AwareModel):
    event_key: str = Field(min_length=1, max_length=100)
    kind: EventKind
    occurred_at: datetime
    actor: str = Field(min_length=1, max_length=80)
    expected_version: int = Field(ge=1)
    department: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=500)
    resolution: Resolution | None = None

    @model_validator(mode="after")
    def shape(self):
        if self.kind == EventKind.ASSIGN and not self.department:
            raise ValueError("Assignment requires department")
        if self.kind == EventKind.CLOSE and self.resolution is None:
            raise ValueError("Closure requires resolution")
        if self.kind in (EventKind.RESPONSE, EventKind.EXPLAIN) and not self.note:
            raise ValueError("Response requires note")
        if self.kind != EventKind.CLOSE and self.resolution is not None:
            raise ValueError("Resolution only applies to closure")
        return self

class ReportInput(AwareModel):
    report_key: str = Field(min_length=1, max_length=100)
    as_of: datetime
    district: str | None = None
    category: str | None = None
    group_by: str = "district"

    @field_validator("group_by")
    @classmethod
    def grouping(cls, value: str) -> str:
        if value not in {"district", "grid", "category", "department"}:
            raise ValueError("Unsupported grouping")
        return value
