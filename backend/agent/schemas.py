"""Pydantic schemas: one argument model per tool plus the shared envelopes.

These models are the single source of truth for:
  * argument validation before any backend call,
  * the Gemini function declarations (generated in ``tool_registry``),
  * the public response shape returned to the UI.

All argument models forbid unknown fields. Timestamps must be timezone-aware
ISO 8601 strings; naive datetimes are rejected (we never *assume* a timezone).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Literal, get_args

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from agent.config import (
    MAX_DAYS_AHEAD,
    MAX_MINUTES,
    MAX_RANGE_DAYS,
    MIN_DAYS_AHEAD,
    MIN_MINUTES,
)

# ---------------------------------------------------------------------------
# Enums and shared aliases
# ---------------------------------------------------------------------------

ToolName = Literal[
    "get_student_context",
    "get_upcoming_deadlines",
    "get_schedule",
    "find_free_slots",
    "create_plan",
    "save_plan",
    "update_task",
    "create_task",
    "replan_schedule",
    "search_notices",
    "log_checkin",
    "get_wellbeing_support",
]
TOOL_NAMES: tuple[str, ...] = get_args(ToolName)

Priority = Literal["low", "medium", "high"]
TaskStatus = Literal["pending", "in_progress", "completed", "postponed"]
CompletionStatus = Literal["completed", "partial", "needs_help"]

# The backend owns ID generation; these are the only accepted shapes.
ID_PREFIXES: dict[str, str] = {
    "plan_id": "plan",
    "task_id": "task",
    "event_id": "event",
}
ID_PATTERN_TEMPLATE = r"^{prefix}_[A-Za-z0-9][A-Za-z0-9_-]{{0,39}}$"
STUDENT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"

# Fields that exist on the args model but must never be visible to the model.
HIDDEN_TOOL_FIELDS: frozenset[str] = frozenset({"student_id", "confirmed_by_user"})


def id_pattern(field_name: str) -> str:
    prefix = ID_PREFIXES[field_name]
    return ID_PATTERN_TEMPLATE.format(prefix=prefix)


def id_regex(field_name: str) -> re.Pattern[str]:
    return re.compile(id_pattern(field_name))


class StrictModel(BaseModel):
    """Base for every model in this module: unknown keys are a hard error."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ToolArgs(StrictModel):
    """Base for tool arguments. ``student_id`` is injected server-side."""

    student_id: str = Field(
        ...,
        pattern=STUDENT_ID_PATTERN,
        description="Internal only: injected by the agent, never model-supplied.",
    )


# ---------------------------------------------------------------------------
# Tool argument models (one per tool, exact names from the frozen contract)
# ---------------------------------------------------------------------------


class GetStudentContextArgs(ToolArgs):
    pass


class GetUpcomingDeadlinesArgs(ToolArgs):
    days_ahead: int = Field(
        default=7, ge=MIN_DAYS_AHEAD, le=MAX_DAYS_AHEAD,
        description="How many days ahead to look (1-30).",
    )


def _check_window(start: datetime | None, end: datetime | None,
                  *, max_days: int = MAX_RANGE_DAYS) -> None:
    if start is None or end is None:
        return
    if end < start:
        raise ValueError("end_date must be on or after start_date")
    if end - start > timedelta(days=max_days):
        raise ValueError(
            f"date range is too wide; ask for a range of {max_days} days or fewer"
        )


class _DateRangeArgs(ToolArgs):
    start_date: AwareDatetime = Field(
        ..., description="ISO 8601 timestamp with offset, e.g. 2026-10-19T00:00:00+05:30"
    )
    end_date: AwareDatetime = Field(
        ..., description="ISO 8601 timestamp with offset, inclusive end of range."
    )

    @model_validator(mode="after")
    def _validate_window(self) -> "_DateRangeArgs":
        _check_window(self.start_date, self.end_date)
        return self


class GetScheduleArgs(_DateRangeArgs):
    pass


class FindFreeSlotsArgs(_DateRangeArgs):
    min_minutes: int = Field(
        default=30, ge=MIN_MINUTES, le=MAX_MINUTES,
        description="Minimum usable slot length in minutes (5-480).",
    )


class CreatePlanArgs(_DateRangeArgs):
    task_ids: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="IDs of tasks/deadlines to plan, as returned by earlier tools.",
    )

    @field_validator("task_ids")
    @classmethod
    def _validate_task_ids(cls, value: list[str]) -> list[str]:
        pattern = id_regex("task_id")
        for task_id in value:
            if not pattern.match(task_id):
                raise ValueError(
                    f"invalid task id {task_id!r}: only IDs returned by earlier "
                    "tool calls may be used"
                )
        return value


class SavePlanArgs(ToolArgs):
    plan_id: str = Field(..., pattern=id_pattern("plan_id"))
    confirmed_by_user: bool = Field(
        default=False,
        description="Internal only: set by confirm_pending(), never model-supplied.",
    )

    @model_validator(mode="after")
    def _must_be_confirmed(self) -> "SavePlanArgs":
        if not self.confirmed_by_user:
            raise ValueError(
                "saving a plan requires explicit user confirmation; "
                "the assistant cannot confirm on the user's behalf"
            )
        return self


class UpdateTaskArgs(StrictModel):
    # No student_id: the frozen contract for update_task has none, so the agent
    # cannot inject one. Ownership is the backend's responsibility there.
    task_id: str = Field(..., pattern=id_pattern("task_id"))
    status: TaskStatus | None = None
    due_at: AwareDatetime | None = None
    estimated_minutes: int | None = Field(default=None, ge=MIN_MINUTES, le=MAX_MINUTES)

    @model_validator(mode="after")
    def _at_least_one_change(self) -> "UpdateTaskArgs":
        if self.status is None and self.due_at is None and self.estimated_minutes is None:
            raise ValueError(
                "provide at least one of status, due_at or estimated_minutes"
            )
        return self


class CreateTaskArgs(ToolArgs):
    title: str = Field(..., min_length=1, max_length=200)
    due_at: AwareDatetime | None = None
    estimated_minutes: int | None = Field(default=None, ge=MIN_MINUTES, le=MAX_MINUTES)
    priority: Priority = "medium"


class ReplanScheduleArgs(ToolArgs):
    plan_id: str = Field(..., pattern=id_pattern("plan_id"))
    change_reason: str = Field(..., min_length=1, max_length=200)
    available_minutes: int | None = Field(
        default=None, ge=0, le=7 * 24 * 60,
        description="Total minutes still available for the plan window.",
    )


class SearchNoticesArgs(ToolArgs):
    query: str = Field(..., min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=40)


class LogCheckinArgs(ToolArgs):
    event_id: str = Field(..., pattern=id_pattern("event_id"))
    completion_status: CompletionStatus
    blocker: str | None = Field(default=None, max_length=300)
    mood: str | None = Field(default=None, max_length=40)
    energy: int | None = Field(default=None, ge=1, le=5)


class GetWellbeingSupportArgs(ToolArgs):
    mood: str | None = Field(default=None, max_length=200)
    risk_flag: bool = False


ARG_MODELS: dict[str, type[ToolArgs]] = {
    "get_student_context": GetStudentContextArgs,
    "get_upcoming_deadlines": GetUpcomingDeadlinesArgs,
    "get_schedule": GetScheduleArgs,
    "find_free_slots": FindFreeSlotsArgs,
    "create_plan": CreatePlanArgs,
    "save_plan": SavePlanArgs,
    "update_task": UpdateTaskArgs,
    "create_task": CreateTaskArgs,
    "replan_schedule": ReplanScheduleArgs,
    "search_notices": SearchNoticesArgs,
    "log_checkin": LogCheckinArgs,
    "get_wellbeing_support": GetWellbeingSupportArgs,
}

# Tools that mutate persistent state. ``save_plan`` is additionally gated by the
# confirmation flow in the controller; this set is informational for the UI.
MUTATING_TOOLS: frozenset[str] = frozenset(
    {"save_plan", "update_task", "create_task", "log_checkin"}
)


# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------


def _json_safe(value: Any, *, what: str) -> Any:
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise ValueError(f"{what} must be JSON-serializable: {exc}") from exc
    return value


class ToolResult(StrictModel):
    """The one shape every tool returns, real backend or mock."""

    success: bool
    tool_name: str = Field(..., min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)
    message: str = Field(...)
    requires_confirmation: bool = False
    next_suggested_tool: str | None = None

    @field_validator("data")
    @classmethod
    def _data_is_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _json_safe(value, what="ToolResult.data")

    @field_validator("message")
    @classmethod
    def _message_is_useful(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("ToolResult.message must not be empty")
        return value


class ToolCallTrace(StrictModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    success: bool = False
    short_result: str = ""
    reason: str | None = None

    @field_validator("args")
    @classmethod
    def _args_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _json_safe(value, what="ToolCallTrace.args")

    @field_validator("short_result")
    @classmethod
    def _truncate(cls, value: str) -> str:
        return value[:400]


class PendingConfirmation(StrictModel):
    plan_id: str
    summary: str
    diff: dict[str, Any] = Field(default_factory=dict)

    @field_validator("diff")
    @classmethod
    def _diff_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _json_safe(value, what="PendingConfirmation.diff")


class AgentResponse(StrictModel):
    reply_text: str
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)
    pending_confirmation: PendingConfirmation | None = None
    error: str | None = None
    used_fallback: bool = False
