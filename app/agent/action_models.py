"""Validated action models for agent tools.

Every tool argument passes through these Pydantic models before any
integration code runs. Unknown actions and out-of-range values are rejected
here, never by the LLM and never by free-form execution.
"""

from __future__ import annotations

import re

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

CalendarActionName = Literal[
    "get_today_events",
    "get_upcoming_events",
    "check_calendar_availability",
    "get_event_details",
    "create_calendar_event",
]

CALENDAR_ACTION_NAMES: tuple[str, ...] = (
    "get_today_events",
    "get_upcoming_events",
    "check_calendar_availability",
    "get_event_details",
    "create_calendar_event",
)

#: Upcoming queries are bounded so one voice question cannot page the API.
MAX_UPCOMING_DAYS = 90
MAX_LIST_RESULTS = 50

#: Pending event-creation confirmations expire after this long.
CONFIRMATION_TTL = timedelta(minutes=5)


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"Not an ISO datetime: {value!r}.") from exc
    else:
        raise ValueError(f"Not an ISO datetime: {value!r}.")
    if parsed.tzinfo is None:
        from app.agent.timezone import local_tz

        parsed = parsed.replace(tzinfo=local_tz())
    return parsed


class UpcomingArgs(BaseModel):
    """Arguments for `get_upcoming_events`."""

    days: int = Field(default=7, ge=1, le=MAX_UPCOMING_DAYS)
    limit: int = Field(default=10, ge=1, le=MAX_LIST_RESULTS)


class AvailabilityArgs(BaseModel):
    """Arguments for `check_calendar_availability`."""

    start: datetime
    end: datetime

    _coerce_start = field_validator("start", mode="before")(_parse_dt)
    _coerce_end = field_validator("end", mode="before")(_parse_dt)

    @model_validator(mode="after")
    def _check_range(self) -> AvailabilityArgs:
        if self.end <= self.start:
            raise ValueError("`end` must be after `start`.")
        if self.end - self.start > timedelta(days=31):
            raise ValueError("Availability windows are limited to 31 days.")
        return self


class EventDetailsArgs(BaseModel):
    """Arguments for `get_event_details`."""

    event_id: str = Field(min_length=1, max_length=512)


class CreateEventArgs(BaseModel):
    """Arguments for `create_calendar_event`; all fields required."""

    summary: str = Field(min_length=1, max_length=500)
    start: datetime
    end: datetime
    description: str = Field(default="", max_length=2000)
    time_zone: str = Field(default="", max_length=100)

    _coerce_start = field_validator("start", mode="before")(_parse_dt)
    _coerce_end = field_validator("end", mode="before")(_parse_dt)

    @model_validator(mode="after")
    def _check_range(self) -> CreateEventArgs:
        if self.end <= self.start:
            raise ValueError("`end` must be after `start`.")
        if self.end - self.start > timedelta(hours=24):
            raise ValueError("Events are limited to 24 hours.")
        return self


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
MAX_EMAIL_RECIPIENTS = 10


def _validate_email(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > 320 or not EMAIL_RE.match(text):
        raise ValueError(f"Not a valid email address: {value!r}.")
    return text


def _validate_recipients(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [part for part in value.replace(";", ",").split(",")]
    if not isinstance(value, list) or not value:
        raise ValueError("At least one recipient is required.")
    if len(value) > MAX_EMAIL_RECIPIENTS:
        raise ValueError(f"At most {MAX_EMAIL_RECIPIENTS} recipients are allowed.")
    cleaned = [_validate_email(item) for item in value]
    if len(set(cleaned)) != len(cleaned):
        raise ValueError("Duplicate recipients are not allowed.")
    return cleaned


def _validate_nonempty(label: str, value: Any, max_length: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} must not be empty.")
    if len(text) > max_length:
        raise ValueError(f"{label} is too long (max {max_length} characters).")
    return text


def _parse_optional_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return _parse_dt(value)


class SendEmailArgs(BaseModel):
    """Arguments for `send_email` (send-only Gmail scope; no inbox access)."""

    to: list[str] = Field(min_length=1, max_length=MAX_EMAIL_RECIPIENTS)
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20000)

    _check_recipients = field_validator("to", mode="before")(_validate_recipients)
    _check_subject = field_validator("subject", mode="before")(
        lambda value: _validate_nonempty("Subject", value, 500)
    )
    _check_body = field_validator("body", mode="before")(
        lambda value: _validate_nonempty("Body", value, 20000)
    )


class ListTasksArgs(BaseModel):
    """Arguments for `get_tasks`."""

    task_list_id: str = Field(default="@default", min_length=1, max_length=256)
    max_results: int = Field(default=20, ge=1, le=100)
    show_completed: bool = False
    due_min: datetime | None = None
    due_max: datetime | None = None

    _coerce_due_min = field_validator("due_min", mode="before")(_parse_optional_dt)
    _coerce_due_max = field_validator("due_max", mode="before")(_parse_optional_dt)

    @model_validator(mode="after")
    def _check_range(self) -> ListTasksArgs:
        if self.due_min is not None and self.due_max is not None and self.due_max <= self.due_min:
            raise ValueError("`due_max` must be after `due_min`.")
        return self


class TaskDetailsArgs(BaseModel):
    """Arguments for `get_task_details`."""

    task_id: str = Field(min_length=1, max_length=1024)
    task_list_id: str = Field(default="@default", min_length=1, max_length=256)


class CreateTaskArgs(BaseModel):
    """Arguments for `create_task`; confirmation required before execution."""

    task_list_id: str = Field(default="@default", min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=1024)
    notes: str = Field(default="", max_length=8192)
    due: datetime | None = None

    _check_title = field_validator("title", mode="before")(
        lambda value: _validate_nonempty("Title", value, 1024)
    )
    _coerce_due = field_validator("due", mode="before")(_parse_optional_dt)


class UpdateTaskArgs(BaseModel):
    """Arguments for `update_task`; confirmation required before execution."""

    task_id: str = Field(min_length=1, max_length=1024)
    task_list_id: str = Field(default="@default", min_length=1, max_length=256)
    title: str | None = Field(default=None, max_length=1024)
    notes: str | None = Field(default=None, max_length=8192)
    due: datetime | None = None
    status: Literal["needsAction", "completed"] | None = None

    _coerce_due = field_validator("due", mode="before")(_parse_optional_dt)

    @model_validator(mode="after")
    def _check_patch(self) -> UpdateTaskArgs:
        if self.title is None and self.notes is None and self.due is None and self.status is None:
            raise ValueError("At least one field to update is required.")
        if isinstance(self.title, str) and not self.title.strip():
            raise ValueError("Title must not be empty.")
        return self


class CompleteTaskArgs(BaseModel):
    """Arguments for `complete_task`; confirmation required before execution."""

    task_id: str = Field(min_length=1, max_length=1024)
    task_list_id: str = Field(default="@default", min_length=1, max_length=256)


ACTION_ARGS_MODELS: dict[str, type[BaseModel]] = {
    "get_today_events": BaseModel,
    "get_upcoming_events": UpcomingArgs,
    "check_calendar_availability": AvailabilityArgs,
    "get_event_details": EventDetailsArgs,
    "create_calendar_event": CreateEventArgs,
    "send_email": SendEmailArgs,
    "list_task_lists": BaseModel,
    "get_tasks": ListTasksArgs,
    "get_task_details": TaskDetailsArgs,
    "create_task": CreateTaskArgs,
    "update_task": UpdateTaskArgs,
    "complete_task": CompleteTaskArgs,
}


class Selection(BaseModel):
    """One validated tool selection from the action-selection step."""

    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    def validated_args(self) -> BaseModel | dict[str, Any]:
        """Validate raw arguments against the action's model."""
        if self.action not in ACTION_ARGS_MODELS:
            raise ValueError(f"Unsupported action: {self.action!r}.")
        model = ACTION_ARGS_MODELS[self.action]
        if model is BaseModel:
            return {}
        return model.model_validate(self.arguments)
