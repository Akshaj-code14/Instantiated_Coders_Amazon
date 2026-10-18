"""Tests for the CampusPilot agent layer (Person 1 / feature branch).

No network calls: the LLM is always a scripted fake.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from agent.schemas import (
    ARG_MODELS,
    TOOL_NAMES,
    AgentResponse,
    CreatePlanArgs,
    CreateTaskArgs,
    FindFreeSlotsArgs,
    GetScheduleArgs,
    GetUpcomingDeadlinesArgs,
    GetWellbeingSupportArgs,
    LogCheckinArgs,
    PendingConfirmation,
    ReplanScheduleArgs,
    SavePlanArgs,
    SearchNoticesArgs,
    ToolCallTrace,
    ToolResult,
    UpdateTaskArgs,
)

IST = timezone(timedelta(hours=5, minutes=30))
START = "2026-10-19T00:00:00+05:30"
END = "2026-10-21T23:59:00+05:30"


def aware(text: str) -> datetime:
    return datetime.fromisoformat(text)


# ---------------------------------------------------------------------------
# Step 1: schemas reject bad input loudly
# ---------------------------------------------------------------------------


def test_every_contract_tool_has_an_args_model() -> None:
    assert set(TOOL_NAMES) == set(ARG_MODELS)
    assert len(TOOL_NAMES) == 12


def test_unknown_extra_field_is_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        GetUpcomingDeadlinesArgs.model_validate(
            {"student_id": "stu_1", "days_ahead": 3, "colour": "red"}
        )
    assert "colour" in str(excinfo.value)


def test_bad_enum_value_is_rejected() -> None:
    with pytest.raises(ValidationError):
        LogCheckinArgs.model_validate(
            {
                "student_id": "stu_1",
                "event_id": "event_ics101",
                "completion_status": "done",
            }
        )
    with pytest.raises(ValidationError):
        CreateTaskArgs.model_validate(
            {"student_id": "stu_1", "title": "Essay", "priority": "urgent"}
        )
    with pytest.raises(ValidationError):
        UpdateTaskArgs.model_validate(
            {"task_id": "task_dbms", "status": "finished"}
        )


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValidationError):
        GetScheduleArgs.model_validate(
            {
                "student_id": "stu_1",
                "start_date": "2026-10-19T00:00:00",
                "end_date": END,
            }
        )


def test_end_before_start_is_rejected() -> None:
    with pytest.raises(ValidationError, match="end_date must be on or after"):
        FindFreeSlotsArgs.model_validate(
            {
                "student_id": "stu_1",
                "start_date": "2026-10-21T00:00:00+05:30",
                "end_date": "2026-10-19T00:00:00+05:30",
                "min_minutes": 30,
            }
        )


def test_date_range_is_capped_at_fourteen_days() -> None:
    with pytest.raises(ValidationError, match="too wide"):
        CreatePlanArgs.model_validate(
            {
                "student_id": "stu_1",
                "start_date": START,
                "end_date": "2026-11-10T00:00:00+05:30",
                "task_ids": ["task_dbms"],
            }
        )


def test_minute_and_day_bounds() -> None:
    with pytest.raises(ValidationError):
        FindFreeSlotsArgs.model_validate(
            {"student_id": "stu_1", "start_date": START, "end_date": END, "min_minutes": 4}
        )
    with pytest.raises(ValidationError):
        FindFreeSlotsArgs.model_validate(
            {"student_id": "stu_1", "start_date": START, "end_date": END, "min_minutes": 481}
        )
    with pytest.raises(ValidationError):
        GetUpcomingDeadlinesArgs.model_validate({"student_id": "stu_1", "days_ahead": 0})
    with pytest.raises(ValidationError):
        GetUpcomingDeadlinesArgs.model_validate({"student_id": "stu_1", "days_ahead": 31})


def test_id_prefixes_are_enforced() -> None:
    with pytest.raises(ValidationError):
        ReplanScheduleArgs.model_validate(
            {
                "student_id": "stu_1",
                "plan_id": "task_dbms",
                "change_reason": "lost two hours",
            }
        )
    with pytest.raises(ValidationError):
        UpdateTaskArgs.model_validate(
            {"task_id": "plan_1", "status": "completed"}
        )
    with pytest.raises(ValidationError):
        LogCheckinArgs.model_validate(
            {
                "student_id": "stu_1",
                "event_id": "ics101",
                "completion_status": "completed",
            }
        )


def test_update_task_requires_a_change() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        UpdateTaskArgs.model_validate({"task_id": "task_dbms"})


def test_save_plan_args_require_confirmation_flag() -> None:
    with pytest.raises(ValidationError, match="explicit user confirmation"):
        SavePlanArgs.model_validate({"student_id": "stu_1", "plan_id": "plan_123"})
    ok = SavePlanArgs.model_validate(
        {"student_id": "stu_1", "plan_id": "plan_123", "confirmed_by_user": True}
    )
    assert ok.confirmed_by_user is True


def test_tool_result_validates_and_is_json_serializable() -> None:
    result = ToolResult(
        success=True,
        tool_name="get_schedule",
        data={"events": [{"id": "event_ics101"}]},
        message="1 class this week",
    )
    assert json.loads(result.model_dump_json())["tool_name"] == "get_schedule"


def test_tool_result_rejects_non_json_data() -> None:
    with pytest.raises(ValidationError):
        ToolResult(
            success=True,
            tool_name="get_schedule",
            data={"start": aware(START)},
            message="bad payload",
        )


def test_agent_response_round_trips_through_json() -> None:
    response = AgentResponse(
        reply_text="Here is a proposal.",
        tool_calls=[
            ToolCallTrace(
                name="create_plan",
                args={"start_date": START},
                success=True,
                short_result="proposal plan_123",
                reason="planning",
            )
        ],
        pending_confirmation=PendingConfirmation(
            plan_id="plan_123", summary="3 blocks", diff={"added": []}
        ),
    )
    restored = AgentResponse.model_validate(json.loads(json.dumps(response.model_dump())))
    assert restored.pending_confirmation is not None
    assert restored.pending_confirmation.plan_id == "plan_123"


def test_optional_fields_default_safely() -> None:
    args = GetWellbeingSupportArgs.model_validate({"student_id": "stu_1"})
    assert args.mood is None and args.risk_flag is False
    notice = SearchNoticesArgs.model_validate({"student_id": "stu_1", "query": "exam"})
    assert notice.category is None


# ---------------------------------------------------------------------------
# Step 2: the mock backend honours the tool contract
# ---------------------------------------------------------------------------

from agent.mock_backend import CampusPilotBackend, MockBackend  # noqa: E402

FIXED_NOW = datetime(2026, 10, 19, 9, 0, tzinfo=IST)
STUDENT = "stu_arjun"
DAY_ONE = datetime(2026, 10, 19, 8, 0, tzinfo=IST)
DAY_THREE = datetime(2026, 10, 22, 22, 0, tzinfo=IST)


def make_backend() -> MockBackend:
    return MockBackend(now=lambda: FIXED_NOW)


def assert_valid_result(result: object, expected_tool: str) -> None:
    assert isinstance(result, ToolResult), f"{expected_tool} returned {type(result)}"
    assert result.tool_name == expected_tool
    ToolResult.model_validate(result.model_dump())
    json.dumps(result.model_dump())


def test_mock_backend_matches_protocol() -> None:
    assert isinstance(make_backend(), CampusPilotBackend)


def test_mock_backend_signatures_match_arg_models() -> None:
    import inspect

    backend = make_backend()
    for tool, model in ARG_MODELS.items():
        method = getattr(backend, tool)
        params = {
            name for name in inspect.signature(method).parameters if name != "self"
        }
        assert params == set(model.model_fields), (
            f"{tool}: backend params {params} != model fields {set(model.model_fields)}"
        )


def test_every_tool_returns_json_serializable_toolresult() -> None:
    backend = make_backend()
    results: list[tuple[str, ToolResult]] = []

    results.append(("get_student_context", backend.get_student_context(student_id=STUDENT)))
    deadlines = backend.get_upcoming_deadlines(student_id=STUDENT, days_ahead=7)
    results.append(("get_upcoming_deadlines", deadlines))
    results.append(("get_schedule", backend.get_schedule(
        student_id=STUDENT, start_date=DAY_ONE, end_date=DAY_THREE)))
    results.append(("find_free_slots", backend.find_free_slots(
        student_id=STUDENT, start_date=DAY_ONE, end_date=DAY_THREE, min_minutes=30)))

    task_ids = [item["id"] for item in deadlines.data["deadlines"]][:2]
    assert len(task_ids) == 2
    plan = backend.create_plan(
        student_id=STUDENT, start_date=DAY_ONE, end_date=DAY_THREE, task_ids=task_ids
    )
    results.append(("create_plan", plan))
    plan_id = plan.data["plan_id"]

    results.append(("save_plan", backend.save_plan(
        student_id=STUDENT, plan_id=plan_id, confirmed_by_user=True)))
    results.append(("update_task", backend.update_task(
        task_id=task_ids[0], status="completed", due_at=None, estimated_minutes=None)))
    created = backend.create_task(
        student_id=STUDENT, title="Practice SQL joins",
        due_at=DAY_THREE, estimated_minutes=45, priority="medium",
    )
    results.append(("create_task", created))
    results.append(("replan_schedule", backend.replan_schedule(
        student_id=STUDENT, plan_id=plan_id,
        change_reason="lost two hours", available_minutes=90)))
    results.append(("search_notices", backend.search_notices(
        student_id=STUDENT, query="assignment", category=None)))
    results.append(("log_checkin", backend.log_checkin(
        student_id=STUDENT, event_id="event_ics101", completion_status="partial",
        blocker="SQL joins were confusing", mood="anxious", energy=2)))
    results.append(("get_wellbeing_support", backend.get_wellbeing_support(
        student_id=STUDENT, mood="stressed", risk_flag=False)))

    assert len(results) == 12
    for expected_tool, result in results:
        assert_valid_result(result, expected_tool)
        assert result.success is True, f"{expected_tool} failed: {result.message}"


def test_mock_backend_rejects_unknown_ids_and_missing_confirmation() -> None:
    backend = make_backend()
    unknown = backend.update_task(
        task_id="task_nope", status="completed", due_at=None, estimated_minutes=None
    )
    assert unknown.success is False and "unknown task" in unknown.message

    plan = backend.create_plan(
        student_id=STUDENT, start_date=DAY_ONE, end_date=DAY_THREE,
        task_ids=["task_dbms"],
    )
    assert plan.success is True and plan.requires_confirmation is True
    unconfirmed = backend.save_plan(
        student_id=STUDENT, plan_id=plan.data["plan_id"], confirmed_by_user=False
    )
    assert unconfirmed.success is False
    assert backend.active_plan_id is None


def test_mock_notices_include_untrusted_injection_payload() -> None:
    result = make_backend().search_notices(
        student_id=STUDENT, query="scholarship", category=None
    )
    assert result.success is True
    notice = result.data["notices"][0]
    assert notice["is_untrusted"] is True
    assert "ignore all previous instructions" in notice["body"]
