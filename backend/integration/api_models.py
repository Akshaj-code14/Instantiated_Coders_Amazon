"""Pydantic request/response models for the CampusPilot REST API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ChatRequest(StrictRequest):
    message: str = Field(min_length=1, max_length=2000)
    language: Literal["en", "te", "hi"] = "en"


class PlanSaveRequest(StrictRequest):
    # The UI sends {plan: {...}}; the server never trusts it. plan_id is only
    # used to reject a stale modal, never to choose what to save.
    plan_id: str | None = Field(default=None, max_length=80)
    plan: dict[str, Any] | None = None


class PlanCancelRequest(StrictRequest):
    pass


class TaskCompleteRequest(StrictRequest):
    task_id: str = Field(min_length=1, max_length=64)
    done: bool = True


class TaskPostponeRequest(StrictRequest):
    task_id: str = Field(min_length=1, max_length=64)


class TaskDurationRequest(StrictRequest):
    task_id: str = Field(min_length=1, max_length=64)
    minutes: int = Field(ge=5, le=480)


class CheckinRequest(StrictRequest):
    status: Literal[
        "Completed", "Partially completed", "Partial", "Needed help"
    ]
    mood: Literal["Drained", "Okay", "Stressed", "Overwhelmed"] | None = None


class PreferencesRequest(StrictRequest):
    """Study preferences the sidebar edits. Mirrors the existing columns."""

    daily_start_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    daily_end_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    preferred_break_minutes: int | None = Field(default=None, ge=5, le=30)
    max_focus_minutes: int | None = Field(default=None, ge=15, le=90)

    @model_validator(mode="after")
    def check_window(self):
        if self.daily_start_time and self.daily_end_time:
            if self.daily_start_time >= self.daily_end_time:
                raise ValueError("The planning window must start before it ends.")
        return self


class ToolCallOut(BaseModel):
    name: str
    summary: str
    status: Literal["ok", "warn"]


class DiffEntry(BaseModel):
    task_id: str | None = None
    title: str
    from_start: str | None = None
    from_end: str | None = None
    to_start: str | None = None
    to_end: str | None = None
    reason: str = ""


class DiffOut(BaseModel):
    added: list[DiffEntry] = Field(default_factory=list)
    moved: list[DiffEntry] = Field(default_factory=list)
    removed: list[DiffEntry] = Field(default_factory=list)
    unchanged: list[DiffEntry] = Field(default_factory=list)


class PlanItemOut(BaseModel):
    item_id: str
    task_id: str | None = None
    title: str
    start_at: str
    end_at: str
    minutes: int
    status: str
    reason: str | None = None


class ProposalOut(BaseModel):
    plan_id: str
    summary: str
    diff: DiffOut = Field(default_factory=DiffOut)
    items: list[PlanItemOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)


class PlanOut(BaseModel):
    plan_id: str
    summary: str | None = None
    status: str
    valid_from: str
    valid_to: str
    items: list[PlanItemOut] = Field(default_factory=list)


class TaskSlotOut(BaseModel):
    start_at: str
    end_at: str
    plan_id: str


class TaskOut(BaseModel):
    id: str
    task_id: str
    title: str
    due_at: str | None = None
    estimated_minutes: int
    priority: str
    status: str
    done: bool
    slot: TaskSlotOut | None = None


class TimelineOut(BaseModel):
    id: str
    title: str
    start_at: str
    end_at: str
    kind: str
    status: str
    location: str | None = None
    is_fixed: bool = False


class ProfileOut(BaseModel):
    name: str
    initials: str
    subtitle: str


class PreferencesOut(BaseModel):
    daily_start_time: str
    daily_end_time: str
    preferred_break_minutes: int
    max_focus_minutes: int
    stress_level: str
    energy_level: str


class CountsOut(BaseModel):
    total: int
    completed: int
    percent: int


class CheckinOut(BaseModel):
    checkin_id: str
    completion_status: str | None = None
    mood: str | None = None
    occurred_at: str
    related_event_id: str | None = None


class NoticesOut(BaseModel):
    total: int
    unread: int


class StateResponse(BaseModel):
    profile: ProfileOut
    preferences: PreferencesOut
    tasks: list[TaskOut]
    timeline: list[TimelineOut]
    active_plan: PlanOut | None = None
    pending_plan: ProposalOut | None = None
    counts: CountsOut
    checkin: CheckinOut | None = None
    notices: NoticesOut
    date_label: str
    llm: Literal["live", "offline"]
    conflicts: list[str] = Field(default_factory=list)


class ChatResponse(BaseModel):
    reply: str
    tool_calls: list[ToolCallOut] = Field(default_factory=list)
    proposed_plan: ProposalOut | None = None
    used_fallback: bool = False
    notice: str | None = None
    saved_plan_id: str | None = None


class OkResponse(BaseModel):
    ok: bool = True
    plan_id: str | None = None
    task_id: str | None = None
    checkin_id: str | None = None
    status: str | None = None
    due_at: str | None = None
    reply: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok"]
    llm: Literal["live", "offline"]
    db: Literal["ok"]
