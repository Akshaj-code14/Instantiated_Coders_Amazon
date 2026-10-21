"""Schedule replanning for CampusPilot.

A replan always creates a *new proposed* plan. The old plan is not changed
until the student confirms the new one.

# INTEGRATION: F4 (Phase 2b) -- the original replanner only recorded
# available_minutes in a summary string, dropped completed items and returned
# no diff. This version enforces the remaining capacity, carries completed and
# past items forward, and returns a real diff for the confirmation modal.
"""

from datetime import datetime, timedelta

from backend.database import get_connection, transaction
from backend.ids import new_id
from backend.planner import create_plan_with_details
from backend.timeutil import now_ist, require_aware, to_iso, to_ist


def _parse(value: str) -> datetime:
    return to_ist(datetime.fromisoformat(value))


def _minutes(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)


def _item_payload(item) -> dict:
    return {
        "item_id": item["plan_item_id"],
        "task_id": item["task_id"],
        "title": item["title"],
        "start_at": item["start_at"],
        "end_at": item["end_at"],
        "minutes": _minutes(_parse(item["start_at"]), _parse(item["end_at"])),
        "status": item["status"],
    }


def _time_range(item) -> tuple[str, str]:
    return item["start_at"], item["end_at"]


def _build_diff(
    *,
    plan_id: str,
    old_items: list,
    new_blocks: list[dict],
    preserved: list[dict],
) -> dict:
    """Compare old future items with the new proposal, task by task."""
    added: list[dict] = []
    moved: list[dict] = []
    removed: list[dict] = []
    unchanged: list[dict] = []

    old_by_task: dict[str, list] = {}
    for item in old_items:
        if item["task_id"]:
            old_by_task.setdefault(item["task_id"], []).append(item)

    new_by_task: dict[str, list[dict]] = {}
    for block in new_blocks:
        new_by_task.setdefault(block["task_id"], []).append(block)

    for preserved_item in preserved:
        unchanged.append(
            {
                "task_id": preserved_item["task_id"],
                "title": preserved_item["title"],
                "from_start": preserved_item["start_at"],
                "from_end": preserved_item["end_at"],
                "to_start": preserved_item["start_at"],
                "to_end": preserved_item["end_at"],
                "reason": "completed or already past; preserved",
            }
        )

    for task_id in sorted(set(old_by_task) | set(new_by_task)):
        old_list = sorted(old_by_task.get(task_id, []), key=lambda item: item["start_at"])
        new_list = sorted(new_by_task.get(task_id, []), key=lambda item: item["start_at"])
        title = (
            new_list[0]["title"] if new_list else old_list[0]["title"]
        )

        if old_list and new_list:
            old_ranges = [_time_range(item) for item in old_list]
            new_ranges = [(item["start_at"], item["end_at"]) for item in new_list]
            if old_ranges == new_ranges:
                unchanged.append(
                    {
                        "task_id": task_id,
                        "title": title,
                        "from_start": old_ranges[0][0],
                        "from_end": old_ranges[0][1],
                        "to_start": new_ranges[0][0],
                        "to_end": new_ranges[0][1],
                        "reason": "unchanged",
                    }
                )
            else:
                moved.append(
                    {
                        "task_id": task_id,
                        "title": title,
                        "from_start": old_ranges[0][0],
                        "from_end": old_ranges[0][1],
                        "to_start": new_ranges[0][0],
                        "to_end": new_ranges[0][1],
                        "reason": "moved to fit the reduced capacity and deadlines",
                    }
                )
        elif new_list:
            added.append(
                {
                    "task_id": task_id,
                    "title": title,
                    "from_start": None,
                    "from_end": None,
                    "to_start": new_list[0]["start_at"],
                    "to_end": new_list[0]["end_at"],
                    "reason": "newly scheduled in this replan",
                }
            )
        else:
            removed.append(
                {
                    "task_id": task_id,
                    "title": title,
                    "from_start": old_list[0]["start_at"],
                    "from_end": old_list[0]["end_at"],
                    "to_start": None,
                    "to_end": None,
                    "reason": "did not fit the remaining capacity",
                }
            )

    return {
        "added": added,
        "moved": moved,
        "removed": removed,
        "unchanged": unchanged,
    }


def replan_schedule_with_details(
    *,
    student_id: str,
    plan_id: str,
    change_reason: str,
    available_minutes: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Create a new proposed plan without modifying the existing plan."""
    if not student_id.strip():
        raise ValueError("student_id cannot be empty.")
    if not plan_id.strip():
        raise ValueError("plan_id cannot be empty.")
    if not change_reason.strip():
        raise ValueError("change_reason cannot be empty.")
    if available_minutes is not None and available_minutes < 0:
        raise ValueError("available_minutes cannot be negative.")

    reference = to_ist(now or now_ist())

    with get_connection() as connection:
        old_plan = connection.execute(
            """
            SELECT plan_id, status, valid_from, valid_to
            FROM plans
            WHERE plan_id = ? AND student_id = ?
            """,
            (plan_id, student_id),
        ).fetchone()

        if old_plan is None:
            raise ValueError("The requested plan was not found for this student.")

        if old_plan["status"] not in ("active", "proposed"):
            raise ValueError("Only an active or proposed plan can be replanned.")

        old_items = connection.execute(
            """
            SELECT plan_item_id, task_id, title, start_at, end_at, status
            FROM plan_items
            WHERE plan_id = ?
            ORDER BY start_at
            """,
            (plan_id,),
        ).fetchall()

        task_rows = connection.execute(
            """
            SELECT task_id, status
            FROM tasks
            WHERE student_id = ?
            """,
            (student_id,),
        ).fetchall()

    if not old_items:
        raise ValueError("The existing plan has no items to replan.")

    start = _parse(old_plan["valid_from"])
    end = _parse(old_plan["valid_to"])
    if end <= start:
        raise ValueError("The existing plan has an invalid time range.")
    if end <= reference:
        raise ValueError("The existing plan window has already ended.")

    task_status = {row["task_id"]: row["status"] for row in task_rows}

    preserved: list[dict] = []
    future_items: list = []
    for item in old_items:
        is_past = _parse(item["end_at"]) <= reference
        is_completed = (
            item["status"] == "completed"
            or (item["task_id"] and task_status.get(item["task_id"]) == "completed")
        )
        if is_past or is_completed:
            preserved.append(_item_payload(item))
        else:
            future_items.append(item)

    task_ids = list(
        dict.fromkeys(
            item["task_id"] for item in future_items if item["task_id"]
        )
    )
    if not task_ids:
        raise ValueError("The existing plan has no unfinished tasks to replan.")

    remaining = sum(
        _minutes(_parse(item["start_at"]), _parse(item["end_at"]))
        for item in future_items
    )
    capacity = remaining if available_minutes is None else max(0, available_minutes)

    # Preserved completed/past blocks must stay blocked so new proposals do
    # not overlap work the student already finished.
    extra_busy = [
        (_parse(item["start_at"]), _parse(item["end_at"]))
        for item in preserved
    ]

    new_plan = create_plan_with_details(
        student_id=student_id,
        start_date=max(start, reference),
        end_date=end,
        task_ids=task_ids,
        max_total_minutes=capacity,
        ignore_plan_ids=[plan_id],
        extra_busy=extra_busy,
        now=reference,
    )
    new_plan_id = new_plan["plan_id"]

    if preserved:
        with get_connection() as connection:
            with transaction(connection):
                for item in preserved:
                    connection.execute(
                        """
                        INSERT INTO plan_items (
                            plan_item_id, plan_id, task_id, event_id,
                            title, start_at, end_at, item_type,
                            priority, status, reason
                        )
                        VALUES (?, ?, ?, NULL, ?, ?, ?, 'study', NULL, ?, ?)
                        """,
                        (
                            new_id("item"),
                            new_plan_id,
                            item["task_id"],
                            item["title"],
                            item["start_at"],
                            item["end_at"],
                            item["status"],
                            f"Preserved from plan {plan_id}: completed/past.",
                        ),
                    )

    diff = _build_diff(
        plan_id=plan_id,
        old_items=future_items,
        new_blocks=new_plan["blocks"],
        preserved=preserved,
    )

    summary = (
        f"Replan reason: {change_reason.strip()}. "
        f"Capacity for this version: {capacity} min. "
        f"{len(diff['added'])} added, {len(diff['moved'])} moved, "
        f"{len(diff['removed'])} removed, {len(diff['unchanged'])} unchanged. "
        "Awaiting student confirmation."
    )
    with get_connection() as connection:
        with transaction(connection):
            connection.execute(
                "UPDATE plans SET summary = ? WHERE plan_id = ? AND student_id = ?",
                (summary, new_plan_id, student_id),
            )

    return {
        **new_plan,
        "plan_id": new_plan_id,
        "summary": summary,
        "available_minutes": capacity,
        "diff": diff,
        "preserved": preserved,
    }


def replan_schedule(
    *,
    student_id: str,
    plan_id: str,
    change_reason: str,
    available_minutes: int | None = None,
) -> str:
    """Create a new proposed plan without modifying the existing plan."""
    return replan_schedule_with_details(
        student_id=student_id,
        plan_id=plan_id,
        change_reason=change_reason,
        available_minutes=available_minutes,
    )["plan_id"]
