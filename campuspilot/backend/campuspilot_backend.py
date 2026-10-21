
"""Main tool adapter for CampusPilot."""

import json
from datetime import datetime, timedelta
from typing import Any, Callable

from backend.database import get_connection, transaction
from backend.timeutil import now_ist, require_aware, to_iso, to_ist
from backend.task_service import create_task as service_create_task
from backend.task_service import update_task as service_update_task
from backend.schedule_service import get_schedule as service_get_schedule
# INTEGRATION: F1/F3 (Phase 2b) -- use the detailed planner/replanner results
# so the adapter can return blocks, warnings, conflicts and a diff.
from backend.planner import (
    create_plan_with_details as service_create_plan_with_details,
    detect_deadline_conflicts,
)
from backend.replanner import (
    replan_schedule_with_details as service_replan_schedule_with_details,
)
from backend.notice_service import search_notices as service_search_notices
from backend.checkin_service import log_checkin as service_log_checkin
from backend.wellbeing_service import (
    get_wellbeing_support as service_get_wellbeing_support,
)
from backend.ids import new_id


def _execute(
    tool_name: str,
    operation: Callable[[], Any],
    *,
    requires_confirmation: bool = False,
    next_suggested_tool: str | None = None,
) -> dict:
    """Run a tool and return the standard response envelope."""
    try:
        data = operation()
        return {
            "success": True,
            "tool_name": tool_name,
            "data": data,
            "message": "Tool completed successfully.",
            "requires_confirmation": requires_confirmation,
            "next_suggested_tool": next_suggested_tool,
        }
    except Exception as exc:
        return {
            "success": False,
            "tool_name": tool_name,
            "data": {},
            "message": str(exc) or "The tool could not complete the request.",
            "requires_confirmation": False,
            "next_suggested_tool": None,
        }


def _check_student(student_id: str) -> None:
    if not isinstance(student_id, str) or not student_id.strip():
        raise ValueError("student_id cannot be empty.")


def _check_datetime(value: datetime, field_name: str) -> datetime:
    return require_aware(value, field_name)


def _clip_payload(value: Any, limit: int = 4000) -> str:
    """Serialize a tool payload without letting a log row grow unbounded."""
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > limit:
        return text[:limit] + "...[truncated]"
    return text


def get_student_context(*, student_id: str) -> dict:
    def operation():
        _check_student(student_id)

        with get_connection() as connection:
            student = connection.execute(
                """
                SELECT student_id, name, email, created_at
                FROM students
                WHERE student_id = ?
                """,
                (student_id,),
            ).fetchone()

            if student is None:
                raise ValueError("Student not found.")

            preferences = connection.execute(
                """
                SELECT *
                FROM preferences
                WHERE student_id = ?
                """,
                (student_id,),
            ).fetchone()

            tasks = connection.execute(
                """
                SELECT task_id, title, description, due_at,
                       estimated_minutes, priority, status, deadline_id
                FROM tasks
                WHERE student_id = ?
                  AND status NOT IN ('completed', 'cancelled')
                ORDER BY due_at IS NULL, due_at
                """,
                (student_id,),
            ).fetchall()

            deadlines = connection.execute(
                """
                SELECT deadline_id, title, due_at, type, course,
                       weight, status
                FROM deadlines
                WHERE student_id = ? AND status = 'open'
                ORDER BY due_at
                """,
                (student_id,),
            ).fetchall()

        return {
            "student": dict(student),
            "preferences": dict(preferences) if preferences else {},
            "tasks": [dict(row) for row in tasks],
            "deadlines": [dict(row) for row in deadlines],
        }

    return _execute("get_student_context", operation)


def get_upcoming_deadlines(
    *,
    student_id: str,
    days_ahead: int,
) -> dict:
    """Return schedulable tasks with their deadline metadata.

    INTEGRATION: F1 (Phase 2b) -- the original returned the deadlines table as
    a bare list. The agent requires a dict envelope and plans with *task* IDs,
    so this returns task rows (with deadline_id/type/course joined in) and both
    ``id`` and ``task_id`` set to the task ID.
    """

    def operation():
        _check_student(student_id)

        if not isinstance(days_ahead, int) or isinstance(days_ahead, bool):
            raise ValueError("days_ahead must be an integer.")

        if days_ahead < 0:
            raise ValueError("days_ahead cannot be negative.")

        now = now_ist()
        cutoff = now + timedelta(days=days_ahead)
        lower_bound = now - timedelta(days=7)

        with get_connection() as connection:
            rows = connection.execute(
                """
                SELECT t.task_id, t.title, t.due_at, t.estimated_minutes,
                       t.priority, t.status, t.deadline_id,
                       d.type, d.course
                FROM tasks AS t
                LEFT JOIN deadlines AS d
                  ON d.deadline_id = t.deadline_id
                 AND d.student_id = t.student_id
                WHERE t.student_id = ?
                  AND t.due_at IS NOT NULL
                  AND t.status NOT IN ('completed', 'cancelled')
                  AND t.due_at >= ?
                  AND t.due_at <= ?
                ORDER BY t.due_at
                """,
                (student_id, to_iso(lower_bound), to_iso(cutoff)),
            ).fetchall()

        items = []
        for row in rows:
            item = dict(row)
            item["id"] = item["task_id"]
            items.append(item)

        return {"deadlines": items}

    return _execute("get_upcoming_deadlines", operation)


def get_schedule(
    *,
    student_id: str,
    start_date: datetime,
    end_date: datetime,
) -> dict:
    """Return events plus committed study blocks from the active plan.

    INTEGRATION: F1 (Phase 2b) -- wrap the list in an ``events`` envelope and
    include the active plan's scheduled blocks, as the tool contract states.
    """

    def operation():
        _check_student(student_id)

        start = _check_datetime(start_date, "start_date")
        end = _check_datetime(end_date, "end_date")

        if end <= start:
            raise ValueError("end_date must be later than start_date.")

        events = service_get_schedule(
            student_id=student_id,
            start_date=start,
            end_date=end,
        )

        with get_connection() as connection:
            plan_rows = connection.execute(
                """
                SELECT pi.plan_item_id AS event_id, pi.title,
                       pi.start_at, pi.end_at, 'study' AS event_type,
                       NULL AS location, 0 AS is_fixed, 'plan' AS source
                FROM plan_items AS pi
                JOIN plans AS p ON p.plan_id = pi.plan_id
                WHERE p.student_id = ?
                  AND p.status = 'active'
                  AND pi.status IN ('scheduled', 'completed')
                  AND pi.start_at < ?
                  AND pi.end_at > ?
                ORDER BY pi.start_at
                """,
                (student_id, to_iso(end), to_iso(start)),
            ).fetchall()

        events.extend(dict(row) for row in plan_rows)
        events.sort(key=lambda item: item["start_at"])
        return {"events": events}

    return _execute("get_schedule", operation)


def find_free_slots(
    *,
    student_id: str,
    start_date: datetime,
    end_date: datetime,
    min_minutes: int,
) -> dict:
    """Find free study slots, leaving breaks between suggested sessions."""

    def operation():
        _check_student(student_id)

        start = to_ist(_check_datetime(start_date, "start_date"))
        end = to_ist(_check_datetime(end_date, "end_date"))

        if end <= start:
            raise ValueError("end_date must be later than start_date.")

        if not isinstance(min_minutes, int) or isinstance(min_minutes, bool):
            raise ValueError("min_minutes must be an integer.")

        if min_minutes <= 0:
            raise ValueError("min_minutes must be positive.")

        with get_connection() as connection:
            preferences = connection.execute(
                """
                SELECT daily_start_time, daily_end_time,
                       max_focus_minutes, preferred_break_minutes
                FROM preferences
                WHERE student_id = ?
                """,
                (student_id,),
            ).fetchone()

            daily_start = (
                datetime.strptime(
                    preferences["daily_start_time"], "%H:%M"
                ).time()
                if preferences
                else datetime.strptime("09:00", "%H:%M").time()
            )

            daily_end = (
                datetime.strptime(
                    preferences["daily_end_time"], "%H:%M"
                ).time()
                if preferences
                else datetime.strptime("22:00", "%H:%M").time()
            )

            max_focus = (
                preferences["max_focus_minutes"]
                if preferences
                else 90
            )

            preferred_break = (
                preferences["preferred_break_minutes"]
                if preferences
                else 15
            )

            if daily_end <= daily_start:
                raise ValueError(
                    "The preferred daily end time must be later than "
                    "the daily start time."
                )

            if (
                not isinstance(max_focus, int)
                or isinstance(max_focus, bool)
                or max_focus <= 0
            ):
                max_focus = 90

            if (
                not isinstance(preferred_break, int)
                or isinstance(preferred_break, bool)
                or preferred_break < 0
            ):
                preferred_break = 15

            events = connection.execute(
                """
                SELECT start_at, end_at
                FROM events
                WHERE student_id = ?
                  AND start_at < ?
                  AND end_at > ?
                """,
                (student_id, to_iso(end), to_iso(start)),
            ).fetchall()

            planned_items = connection.execute(
                """
                SELECT pi.start_at, pi.end_at
                FROM plan_items AS pi
                JOIN plans AS p ON p.plan_id = pi.plan_id
                WHERE p.student_id = ?
                  AND p.status IN ('active', 'proposed')
                  AND pi.status IN ('proposed', 'scheduled', 'completed')
                  AND pi.start_at < ?
                  AND pi.end_at > ?
                """,
                (student_id, to_iso(end), to_iso(start)),
            ).fetchall()

        busy = []

        for row in list(events) + list(planned_items):
            busy_start = to_ist(
                datetime.fromisoformat(row["start_at"])
            )
            busy_end = to_ist(
                datetime.fromisoformat(row["end_at"])
            )

            busy.append((
                max(start, busy_start),
                min(end, busy_end),
            ))

        busy.sort(key=lambda interval: interval[0])
        merged = []

        for busy_start, busy_end in busy:
            if merged and busy_start <= merged[-1][1]:
                merged[-1] = (
                    merged[-1][0],
                    max(merged[-1][1], busy_end),
                )
            else:
                merged.append((busy_start, busy_end))

        free_slots = []
        current_date = start.date()

        while current_date <= end.date():
            day_start = datetime.combine(
                current_date,
                daily_start,
                tzinfo=start.tzinfo,
            )
            day_end = datetime.combine(
                current_date,
                daily_end,
                tzinfo=start.tzinfo,
            )

            window_start = max(start, day_start)
            window_end = min(end, day_end)

            if window_end > window_start:
                day_busy = [
                    (
                        max(window_start, busy_start),
                        min(window_end, busy_end),
                    )
                    for busy_start, busy_end in merged
                    if busy_start < window_end
                    and busy_end > window_start
                ]

                cursor = window_start
                gaps = []

                for busy_start, busy_end in day_busy:
                    if busy_start > cursor:
                        gaps.append((cursor, busy_start))

                    cursor = max(cursor, busy_end)

                if cursor < window_end:
                    gaps.append((cursor, window_end))

                for gap_start, gap_end in gaps:
                    slot_start = gap_start

                    while slot_start < gap_end:
                        slot_end = min(
                            slot_start + timedelta(minutes=max_focus),
                            gap_end,
                        )

                        duration = int(
                            (slot_end - slot_start).total_seconds() // 60
                        )

                        if duration >= min_minutes:
                            free_slots.append({
                                "start_at": to_iso(slot_start),
                                "end_at": to_iso(slot_end),
                                "duration_minutes": duration,
                            })

                        if slot_end >= gap_end:
                            break

                        next_slot_start = slot_end + timedelta(
                            minutes=preferred_break
                        )

                        if next_slot_start <= slot_start:
                            break

                        slot_start = next_slot_start

            current_date += timedelta(days=1)

        conflicts = detect_deadline_conflicts(
            student_id=student_id,
            start_date=start,
            end_date=end,
        )
        # INTEGRATION: F1 (Phase 2b) -- dict envelope + conflicts expected by
        # the agent's fallback router and the system prompt.
        return {"free_slots": free_slots, "conflicts": conflicts}

    return _execute("find_free_slots", operation)


def create_plan(
    *,
    student_id: str,
    start_date: datetime,
    end_date: datetime,
    task_ids: list[str],
) -> dict:
    def operation():
        _check_student(student_id)

        start = _check_datetime(start_date, "start_date")
        end = _check_datetime(end_date, "end_date")

        if end <= start:
            raise ValueError("end_date must be later than start_date.")

        if not isinstance(task_ids, list):
            raise ValueError("task_ids must be a list of task IDs.")

        # INTEGRATION: F3 (Phase 2b) -- expose blocks/total/warnings/conflicts
        # so pending_confirmation and the review modal contain real data.
        return service_create_plan_with_details(
            student_id=student_id,
            start_date=start,
            end_date=end,
            task_ids=task_ids,
        )

    return _execute(
        "create_plan",
        operation,
        requires_confirmation=True,
        next_suggested_tool="save_plan",
    )


def save_plan(
    *,
    student_id: str,
    plan_id: str,
    confirmed_by_user: bool,
) -> dict:
    """Confirm a proposed plan after checking for real scheduling conflicts."""

    def operation():
        _check_student(student_id)

        if not isinstance(plan_id, str) or not plan_id.strip():
            raise ValueError("plan_id cannot be empty.")

        if not isinstance(confirmed_by_user, bool):
            raise ValueError("confirmed_by_user must be a boolean.")

        if not confirmed_by_user:
            return {
                "plan_id": plan_id,
                "status": "proposed",
                "message": (
                    "The plan remains proposed because "
                    "confirmation was not given."
                ),
            }

        with get_connection() as connection:
            with transaction(connection):
                plan = connection.execute(
                    """
                    SELECT plan_id, status
                    FROM plans
                    WHERE plan_id = ? AND student_id = ?
                    """,
                    (plan_id, student_id),
                ).fetchone()

                if plan is None:
                    raise ValueError("Plan not found for this student.")

                # INTEGRATION: F5 (Phase 2b) -- a repeated confirm is a no-op,
                # not a duplicate plan and not an error.
                if plan["status"] == "active":
                    return {
                        "plan_id": plan_id,
                        "status": "active",
                        "already_active": True,
                    }

                if plan["status"] != "proposed":
                    raise ValueError(
                        "Only a proposed plan can be confirmed."
                    )

                proposed_items = connection.execute(
                    """
                    SELECT plan_item_id, start_at, end_at, title
                    FROM plan_items
                    WHERE plan_id = ? AND status = 'proposed'
                    ORDER BY start_at
                    """,
                    (plan_id,),
                ).fetchall()

                if not proposed_items:
                    raise ValueError(
                        "Cannot confirm this plan because it has no "
                        "proposed study items."
                    )

                for item in proposed_items:
                    event_conflict = connection.execute(
                        """
                        SELECT title
                        FROM events
                        WHERE student_id = ?
                          AND start_at < ?
                          AND end_at > ?
                        LIMIT 1
                        """,
                        (
                            student_id,
                            item["end_at"],
                            item["start_at"],
                        ),
                    ).fetchone()

                    if event_conflict is not None:
                        raise ValueError(
                            f"Cannot confirm plan: '{item['title']}' "
                            f"overlaps calendar event "
                            f"'{event_conflict['title']}'."
                        )

                    completed_conflict = connection.execute(
                        """
                        SELECT pi.title
                        FROM plan_items AS pi
                        JOIN plans AS p ON p.plan_id = pi.plan_id
                        WHERE p.student_id = ?
                          AND p.plan_id != ?
                          AND pi.status = 'completed'
                          AND pi.start_at < ?
                          AND pi.end_at > ?
                        LIMIT 1
                        """,
                        (
                            student_id,
                            plan_id,
                            item["end_at"],
                            item["start_at"],
                        ),
                    ).fetchone()

                    if completed_conflict is not None:
                        raise ValueError(
                            f"Cannot confirm plan: '{item['title']}' "
                            f"overlaps completed study item "
                            f"'{completed_conflict['title']}'."
                        )

                    scheduled_conflict = connection.execute(
                        """
                        SELECT pi.title
                        FROM plan_items AS pi
                        JOIN plans AS p ON p.plan_id = pi.plan_id
                        WHERE p.student_id = ?
                          AND p.plan_id != ?
                          AND p.status = 'active'
                          -- INTEGRATION: F5 (Phase 2b): the currently active
                          -- plan is superseded by this confirmation, so it
                          -- must not block a replan of the same slots.
                          AND p.plan_id NOT IN (
                              SELECT plan_id FROM plans
                              WHERE student_id = ? AND status = 'active'
                          )
                          AND pi.status = 'scheduled'
                          AND pi.start_at < ?
                          AND pi.end_at > ?
                        LIMIT 1
                        """,
                        (
                            student_id,
                            plan_id,
                            student_id,
                            item["end_at"],
                            item["start_at"],
                        ),
                    ).fetchone()

                    if scheduled_conflict is not None:
                        raise ValueError(
                            f"Cannot confirm plan: '{item['title']}' "
                            f"overlaps scheduled item "
                            f"'{scheduled_conflict['title']}'."
                        )

                # All validations passed. Only now replace the old active plan.
                connection.execute(
                    """
                    UPDATE plans
                    SET status = 'superseded'
                    WHERE student_id = ? AND status = 'active'
                    """,
                    (student_id,),
                )

                connection.execute(
                    """
                    UPDATE plans
                    SET status = 'active',
                        summary = REPLACE(
                            REPLACE(
                                REPLACE(
                                    COALESCE(summary, ''),
                                    ' Student confirmation required.',
                                    ''
                                ),
                                'Awaiting student confirmation.',
                                ''
                            ),
                            'awaiting student confirmation',
                            'confirmed by student'
                        )
                    WHERE plan_id = ? AND student_id = ?
                    """,
                    (plan_id, student_id),
                )

                connection.execute(
                    """
                    UPDATE plan_items
                    SET status = 'scheduled',
                        reason = REPLACE(
                            COALESCE(reason, ''),
                            'awaiting confirmation',
                            'confirmed by student'
                        )
                    WHERE plan_id = ? AND status = 'proposed'
                    """,
                    (plan_id,),
                )

        return {
            "plan_id": plan_id,
            "status": "active",
        }

    return _execute(
        "save_plan",
        operation,
        requires_confirmation=False,
        next_suggested_tool="get_active_plan",
    )


def update_task(
    *,
    task_id: str,
    status: str | None,
    due_at: datetime | None,
    estimated_minutes: int | None,
    student_id: str | None = None,
) -> dict:
    """Update a task, optionally scoped to the session's student.

    INTEGRATION: F6 (Phase 2b) -- the frozen agent contract has no student_id
    on this tool, so the server's scoped wrapper passes it here; the service
    then adds ``WHERE student_id = ?``. Direct adapter callers may omit it.
    """

    def operation():
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id cannot be empty.")

        if student_id is not None:
            _check_student(student_id)

        if due_at is not None:
            _check_datetime(due_at, "due_at")

        updated = service_update_task(
            task_id=task_id,
            status=status,
            due_at=due_at,
            estimated_minutes=estimated_minutes,
            student_id=student_id,
        )

        if not updated:
            raise ValueError("Task not found or no fields were updated.")

        with get_connection() as connection:
            row = connection.execute(
                """
                SELECT task_id, title, status, due_at, estimated_minutes, priority
                FROM tasks WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()

        return {
            "task_id": task_id,
            "updated": True,
            "status": row["status"] if row else status,
            "due_at": row["due_at"] if row else None,
            "estimated_minutes": row["estimated_minutes"] if row else None,
            "title": row["title"] if row else None,
        }

    return _execute("update_task", operation)


def create_task(
    *,
    student_id: str,
    title: str,
    due_at: datetime | None,
    estimated_minutes: int | None,
    priority: str,
) -> dict:
    def operation():
        _check_student(student_id)

        if due_at is not None:
            _check_datetime(due_at, "due_at")

        task_id = service_create_task(
            student_id=student_id,
            title=title,
            due_at=due_at,
            estimated_minutes=estimated_minutes,
            priority=priority,
        )

        with get_connection() as connection:
            row = connection.execute(
                """
                SELECT task_id, title, due_at, estimated_minutes, priority, status
                FROM tasks WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()

        return {
            "task_id": task_id,
            "title": row["title"] if row else title,
            "due_at": row["due_at"] if row else None,
            "estimated_minutes": row["estimated_minutes"] if row else None,
            "priority": row["priority"] if row else priority,
            "status": row["status"] if row else "pending",
        }

    return _execute("create_task", operation)


def replan_schedule(
    *,
    student_id: str,
    plan_id: str,
    change_reason: str,
    available_minutes: int | None,
) -> dict:
    def operation():
        _check_student(student_id)

        return service_replan_schedule_with_details(
            student_id=student_id,
            plan_id=plan_id,
            change_reason=change_reason,
            available_minutes=available_minutes,
        )

    return _execute(
        "replan_schedule",
        operation,
        requires_confirmation=True,
        next_suggested_tool="save_plan",
    )


def search_notices(
    *,
    student_id: str,
    query: str,
    category: str | None,
) -> dict:
    def operation():
        _check_student(student_id)

        # INTEGRATION: F1 (Phase 2b) -- dict envelope required by ToolResult.
        return {
            "notices": service_search_notices(
                student_id=student_id,
                query=query,
                category=category,
            )
        }

    return _execute("search_notices", operation)


def log_checkin(
    *,
    student_id: str,
    event_id: str | None,
    completion_status: str,
    blocker: str | None,
    mood: str | None,
    energy: int | None,
) -> dict:
    def operation():
        _check_student(student_id)

        checkin_id = service_log_checkin(
            student_id=student_id,
            event_id=event_id,
            completion_status=completion_status,
            blocker=blocker,
            mood=mood,
            energy=energy,
        )

        return {
            "checkin_id": checkin_id,
            "event_id": event_id,
            "blocker": blocker,
            "completion_status": completion_status,
        }

    return _execute("log_checkin", operation)


def get_wellbeing_support(
    *,
    student_id: str,
    mood: str | None,
    risk_flag: bool,
) -> dict:
    def operation():
        _check_student(student_id)

        data = service_get_wellbeing_support(
            student_id=student_id,
            mood=mood,
            risk_flag=risk_flag,
        )
        # INTEGRATION: F7 (Phase 2b) -- the agent renders data["resources"].
        # These are public India-wide helplines, not invented campus contacts.
        data["resources"] = [
            {"name": "Tele-MANAS", "contact": "14416",
             "hours": "24/7, India"},
            {"name": "iCall (TISS)", "contact": "9152987821",
             "hours": "Mon-Sat, 10:00-20:00 IST"},
            {"name": "AASRA", "contact": "9820466726",
             "hours": "24/7, India"},
        ]
        return data

    return _execute("get_wellbeing_support", operation)


def get_active_plan(student_id: str) -> dict:
    def operation():
        _check_student(student_id)

        with get_connection() as connection:
            plan = connection.execute(
                """
                SELECT *
                FROM plans
                WHERE student_id = ? AND status = 'active'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (student_id,),
            ).fetchone()

            if plan is None:
                return {"plan": None, "items": []}

            items = connection.execute(
                """
                SELECT *
                FROM plan_items
                WHERE plan_id = ?
                ORDER BY start_at
                """,
                (plan["plan_id"],),
            ).fetchall()

        return {
            "plan": dict(plan),
            "items": [dict(item) for item in items],
        }

    return _execute("get_active_plan", operation)


def get_pending_plan(student_id: str) -> dict:
    def operation():
        _check_student(student_id)

        with get_connection() as connection:
            plan = connection.execute(
                """
                SELECT *
                FROM plans
                WHERE student_id = ? AND status = 'proposed'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (student_id,),
            ).fetchone()

            if plan is None:
                return {"plan": None, "items": []}

            items = connection.execute(
                """
                SELECT *
                FROM plan_items
                WHERE plan_id = ?
                ORDER BY start_at
                """,
                (plan["plan_id"],),
            ).fetchall()

        return {
            "plan": dict(plan),
            "items": [dict(item) for item in items],
        }

    return _execute("get_pending_plan", operation)


def log_tool_call(
    *,
    student_id: str | None,
    tool_name: str,
    arguments: dict,
    result: dict,
) -> dict:
    """Persist a tool invocation result for debugging and auditing."""

    def operation():
        log_id = new_id("log")

        with get_connection() as connection:
            with transaction(connection):
                connection.execute(
                    """
                    INSERT INTO tool_logs (
                        log_id, student_id, tool_name, arguments_json,
                        result_json, success, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        log_id,
                        student_id,
                        tool_name,
                        _clip_payload(arguments),
                        _clip_payload(result),
                        int(bool(result.get("success"))),
                        to_iso(now_ist()),
                    ),
                )

        return {"log_id": log_id}

    return _execute("log_tool_call", operation)
