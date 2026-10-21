"""Deterministic study-plan generation for CampusPilot.

Plans remain proposed until explicitly confirmed by the student.
Study sessions respect fixed calendar events, each task's deadline, the
configured focus limit and the break between sessions.

# INTEGRATION: F2 (Phase 2b) -- the original planner ordered tasks by label,
# ignored due_at during placement, never split blocks at max_focus_minutes and
# only counted unscheduled work. This version implements the A5 rules while
# keeping the public ``create_plan(...) -> plan_id`` signature used by
# replanner.py and tests/test_backend.py. ``create_plan_with_details`` is the
# additive entry point used by the adapter (F3).
"""

from datetime import datetime, time, timedelta

from backend.database import get_connection, transaction
from backend.ids import new_id
from backend.timeutil import IST, now_ist, require_aware, to_iso, to_ist

STEP_MINUTES = 15
CONFLICT_LOOKBACK_MINUTES = 30
IMPORTANCE = {"high": 5, "medium": 3, "low": 1}


def _parse_due(value: str | None) -> datetime | None:
    if not value:
        return None
    return to_ist(datetime.fromisoformat(value))


def _chunks(duration: int, max_focus: int) -> list[int]:
    """Split a task into near-equal blocks no longer than max_focus."""
    if duration <= max_focus:
        return [duration]
    count = max(1, (duration + max_focus - 1) // max_focus)
    base, remainder = divmod(duration, count)
    return [base + 1 if index < remainder else base for index in range(count)]


def _priority_score(task, now: datetime) -> float:
    """A5 priority score: deadline pressure + importance + size + overdue."""
    due = _parse_due(task["due_at"])
    days_until_due = (due.date() - now.date()).days if due else 0
    importance = IMPORTANCE.get(task["priority"], 1)
    minutes = int(task["estimated_minutes"] or 30)
    overdue = 10 if due is not None and due < now else 0
    return max(0, 10 - days_until_due) + importance + minutes / 60 + overdue


def _sort_key(task, now: datetime):
    due = _parse_due(task["due_at"])
    return (
        -_priority_score(task, now),
        due or datetime.max.replace(tzinfo=IST),
        int(task["estimated_minutes"] or 30),
        task["task_id"],
    )


def _find_slots(
    chunks: list[int],
    *,
    window_start: datetime,
    latest_end: datetime,
    daily_start: time,
    daily_end: time,
    break_minutes: int,
    busy: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]] | None:
    """Find consecutive slots for every chunk, or None if the task cannot fit."""
    if not chunks:
        return []

    reserved = list(busy)
    placed: list[tuple[datetime, datetime]] = []
    search_from = window_start

    for chunk in chunks:
        day = search_from.date()
        found: tuple[datetime, datetime] | None = None

        while day <= latest_end.date() and found is None:
            day_start = datetime.combine(day, daily_start, tzinfo=window_start.tzinfo)
            day_end = datetime.combine(day, daily_end, tzinfo=window_start.tzinfo)
            slot_start = max(search_from, day_start)
            available_end = min(latest_end, day_end)
            latest_start = available_end - timedelta(minutes=chunk)

            while slot_start <= latest_start:
                slot_end = slot_start + timedelta(minutes=chunk)
                overlaps = any(
                    slot_start < busy_end and slot_end > busy_start
                    for busy_start, busy_end in reserved
                )
                if not overlaps:
                    found = (slot_start, slot_end)
                    break
                slot_start += timedelta(minutes=STEP_MINUTES)

            day += timedelta(days=1)

        if found is None:
            return None

        placed.append(found)
        reserved.append(found)
        if break_minutes > 0:
            reserved.append(
                (found[1], found[1] + timedelta(minutes=break_minutes))
            )
            search_from = found[1] + timedelta(minutes=break_minutes)
        else:
            search_from = found[1]

    return placed


def _free_minutes_before(
    busy: list[tuple[datetime, datetime]],
    start: datetime,
    end: datetime,
) -> int:
    if end <= start:
        return 0
    clipped = sorted(
        (max(start, busy_start), min(end, busy_end))
        for busy_start, busy_end in busy
        if busy_start < end and busy_end > start
    )
    total = 0
    cursor = start
    for busy_start, busy_end in clipped:
        if busy_start > cursor:
            total += int((busy_start - cursor).total_seconds() // 60)
        cursor = max(cursor, busy_end)
    if cursor < end:
        total += int((end - cursor).total_seconds() // 60)
    return total


def _load_busy(
    connection,
    student_id: str,
    start: datetime,
    end: datetime,
    ignore_plan_ids: list[str] | None,
    extra_busy: list[tuple[datetime, datetime]] | None = None,
) -> list[tuple[datetime, datetime]]:
    busy: list[tuple[datetime, datetime]] = list(extra_busy or [])

    for row in connection.execute(
        """
        SELECT start_at, end_at
        FROM events
        WHERE student_id = ? AND start_at < ? AND end_at > ?
        """,
        (student_id, to_iso(end), to_iso(start)),
    ).fetchall():
        busy.append(
            (
                to_ist(datetime.fromisoformat(row["start_at"])),
                to_ist(datetime.fromisoformat(row["end_at"])),
            )
        )

    sql = """
        SELECT pi.start_at, pi.end_at
        FROM plan_items AS pi
        JOIN plans AS p ON p.plan_id = pi.plan_id
        WHERE p.student_id = ?
          AND p.status = 'active'
          AND pi.status = 'scheduled'
          AND pi.start_at < ?
          AND pi.end_at > ?
    """
    parameters: list[object] = [student_id, to_iso(end), to_iso(start)]
    ignored = [plan_id for plan_id in (ignore_plan_ids or []) if plan_id]
    if ignored:
        placeholders = ",".join("?" for _ in ignored)
        sql += f" AND p.plan_id NOT IN ({placeholders})"
        parameters.extend(ignored)

    for row in connection.execute(sql, parameters).fetchall():
        busy.append(
            (
                to_ist(datetime.fromisoformat(row["start_at"])),
                to_ist(datetime.fromisoformat(row["end_at"])),
            )
        )

    busy.sort(key=lambda interval: interval[0])
    return busy


def detect_deadline_conflicts(
    *,
    student_id: str,
    start_date: datetime,
    end_date: datetime,
    task_ids: list[str] | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Report deadlines that fall inside / right after a fixed event."""
    require_aware(start_date, "start_date")
    require_aware(end_date, "end_date")
    start = to_ist(start_date)
    end = to_ist(end_date)
    reference = to_ist(now or now_ist())

    with get_connection() as connection:
        sql = """
            SELECT task_id, title, due_at
            FROM tasks
            WHERE student_id = ?
              AND due_at IS NOT NULL
              AND status NOT IN ('completed', 'cancelled')
              AND due_at <= ?
        """
        parameters: list[object] = [student_id, to_iso(end)]
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            sql += f" AND task_id IN ({placeholders})"
            parameters.extend(task_ids)

        tasks = connection.execute(sql, parameters).fetchall()
        events = connection.execute(
            """
            SELECT title, start_at, end_at
            FROM events
            WHERE student_id = ? AND start_at < ? AND end_at > ?
            ORDER BY start_at
            """,
            (
                student_id,
                to_iso(end + timedelta(days=1)),
                to_iso(start - timedelta(days=1)),
            ),
        ).fetchall()

    conflicts: list[dict] = []
    for task in tasks:
        due = _parse_due(task["due_at"])
        if due is None:
            continue

        for event in events:
            event_start = to_ist(datetime.fromisoformat(event["start_at"]))
            event_end = to_ist(datetime.fromisoformat(event["end_at"]))

            if event_start < due and event_end >= due - timedelta(
                minutes=CONFLICT_LOOKBACK_MINUTES
            ):
                gap = int((due - event_end).total_seconds() // 60)
                if gap < 0:
                    note = (
                        f"{task['title']} is due at {due:%H:%M} on {due:%a}, "
                        f"while '{event['title']}' is still running until "
                        f"{event_end:%H:%M}."
                    )
                else:
                    note = (
                        f"{task['title']} is due at {due:%H:%M} on {due:%a}, "
                        f"only {gap} min after '{event['title']}' ends at "
                        f"{event_end:%H:%M}."
                    )
                conflicts.append(
                    {
                        "task_id": task["task_id"],
                        "title": task["title"],
                        "due_at": to_iso(due),
                        "event_title": event["title"],
                        "event_end": to_iso(event_end),
                        "gap_minutes": gap,
                        "note": note,
                    }
                )
                break

    return conflicts


def create_plan_with_details(
    *,
    student_id: str,
    start_date: datetime,
    end_date: datetime,
    task_ids: list[str],
    max_total_minutes: int | None = None,
    ignore_plan_ids: list[str] | None = None,
    extra_busy: list[tuple[datetime, datetime]] | None = None,
    now: datetime | None = None,
) -> dict:
    """Create a proposed study plan and return everything the UI needs."""
    if not isinstance(student_id, str) or not student_id.strip():
        raise ValueError("student_id cannot be empty.")

    require_aware(start_date, "start_date")
    require_aware(end_date, "end_date")

    start = to_ist(start_date)
    end = to_ist(end_date)
    reference = to_ist(now or now_ist())

    if end <= start:
        raise ValueError("end_date must be after start_date.")

    if len(task_ids) != len(set(task_ids)):
        raise ValueError("task_ids must not contain duplicates.")

    plan_id = new_id("plan")
    blocks: list[dict] = []
    warnings: list[str] = []
    unscheduled_task_ids: list[str] = []
    planned_minutes = 0

    with get_connection() as connection:
        with transaction(connection):
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                tasks = connection.execute(
                    f"""
                    SELECT task_id, title, due_at, estimated_minutes, priority
                    FROM tasks
                    WHERE student_id = ?
                      AND status NOT IN ('completed', 'cancelled')
                      AND task_id IN ({placeholders})
                    """,
                    [student_id, *task_ids],
                ).fetchall()

                if len(tasks) != len(task_ids):
                    raise ValueError(
                        "One or more tasks do not exist for this student "
                        "or are already completed/cancelled."
                    )
            else:
                tasks = []

            preference = connection.execute(
                """
                SELECT daily_start_time, daily_end_time,
                       preferred_break_minutes, max_focus_minutes
                FROM preferences
                WHERE student_id = ?
                """,
                (student_id,),
            ).fetchone()

            if preference:
                daily_start_time = time.fromisoformat(preference["daily_start_time"])
                daily_end_time = time.fromisoformat(preference["daily_end_time"])
                break_minutes = int(preference["preferred_break_minutes"] or 0)
                max_focus = int(preference["max_focus_minutes"] or 90)
            else:
                daily_start_time = time(9, 0)
                daily_end_time = time(22, 0)
                break_minutes = 15
                max_focus = 90

            if break_minutes < 0:
                raise ValueError("preferred_break_minutes cannot be negative.")
            if max_focus <= 0:
                max_focus = 90
            if daily_start_time >= daily_end_time:
                raise ValueError(
                    "Daily start time must be earlier than daily end time."
                )

            effective_start = max(start, reference)
            busy = _load_busy(
                connection, student_id, effective_start, end, ignore_plan_ids,
                extra_busy,
            )

            connection.execute(
                """
                INSERT INTO plans (
                    plan_id, student_id, created_at,
                    valid_from, valid_to, status, summary
                )
                VALUES (?, ?, ?, ?, ?, 'proposed', ?)
                """,
                (
                    plan_id,
                    student_id,
                    to_iso(reference),
                    to_iso(start),
                    to_iso(end),
                    "Proposed plan awaiting student confirmation.",
                ),
            )

            for task in sorted(tasks, key=lambda item: _sort_key(item, reference)):
                duration = int(task["estimated_minutes"] or 30)
                if duration <= 0:
                    duration = 30

                due = _parse_due(task["due_at"])
                latest_end = min(end, due) if due is not None else end

                if latest_end <= effective_start:
                    warnings.append(
                        f"{task['title']} could not be scheduled: its deadline "
                        f"({due:%a %H:%M}) has already passed."
                        if due
                        else f"{task['title']} could not be scheduled: the plan "
                        "window is empty."
                    )
                    unscheduled_task_ids.append(task["task_id"])
                    continue

                scheduled_duration = duration
                if max_total_minutes is not None:
                    remaining_capacity = max(
                        0, max_total_minutes - planned_minutes
                    )
                    if remaining_capacity < 5:
                        warnings.append(
                            f"{task['title']} did not fit in the remaining "
                            f"{remaining_capacity} min capacity."
                        )
                        unscheduled_task_ids.append(task["task_id"])
                        continue
                    if duration > remaining_capacity:
                        scheduled_duration = remaining_capacity
                        warnings.append(
                            f"{task['title']} only partially fit: "
                            f"{scheduled_duration} of {duration} min were "
                            "scheduled; the rest was left out."
                        )

                slots = _find_slots(
                    _chunks(scheduled_duration, max_focus),
                    window_start=effective_start,
                    latest_end=latest_end,
                    daily_start=daily_start_time,
                    daily_end=daily_end_time,
                    break_minutes=break_minutes,
                    busy=busy,
                )

                if slots is None:
                    free_minutes = _free_minutes_before(
                        busy, effective_start, latest_end
                    )
                    deadline = (
                        f" before the {due:%a %H:%M} deadline" if due else ""
                    )
                    warnings.append(
                        f"{task['title']} needs {duration} min but only "
                        f"{free_minutes} min are free{deadline}; it was not "
                        "scheduled."
                    )
                    unscheduled_task_ids.append(task["task_id"])
                    continue

                for slot_start, slot_end in slots:
                    minutes = int((slot_end - slot_start).total_seconds() // 60)
                    item_id = new_id("item")
                    connection.execute(
                        """
                        INSERT INTO plan_items (
                            plan_item_id, plan_id, task_id, event_id,
                            title, start_at, end_at, item_type,
                            priority, status, reason
                        )
                        VALUES (?, ?, ?, NULL, ?, ?, ?, 'study',
                                ?, 'proposed', ?)
                        """,
                        (
                            item_id,
                            plan_id,
                            task["task_id"],
                            task["title"],
                            to_iso(slot_start),
                            to_iso(slot_end),
                            task["priority"],
                            "Proposed study session; awaiting confirmation.",
                        ),
                    )
                    blocks.append(
                        {
                            "item_id": item_id,
                            "task_id": task["task_id"],
                            "title": task["title"],
                            "start_at": to_iso(slot_start),
                            "end_at": to_iso(slot_end),
                            "minutes": minutes,
                            "status": "proposed",
                        }
                    )
                    planned_minutes += minutes
                    busy.append((slot_start, slot_end))
                    if break_minutes > 0:
                        busy.append(
                            (
                                slot_end,
                                slot_end + timedelta(minutes=break_minutes),
                            )
                        )

            conflicts = detect_deadline_conflicts(
                student_id=student_id,
                start_date=effective_start,
                end_date=end,
                task_ids=task_ids or None,
                now=reference,
            )

            summary = (
                f"{len(blocks)} study session(s) proposed; "
                f"{len(unscheduled_task_ids)} task(s) could not be scheduled; "
                f"{len(conflicts)} conflict(s). Student confirmation required."
            )
            connection.execute(
                "UPDATE plans SET summary = ? WHERE plan_id = ?",
                (summary, plan_id),
            )

    return {
        "plan_id": plan_id,
        "status": "proposed",
        "blocks": blocks,
        "total_minutes": planned_minutes,
        "warnings": warnings,
        "conflicts": conflicts,
        "unscheduled_task_ids": unscheduled_task_ids,
        "summary": summary,
    }


def create_plan(
    *,
    student_id: str,
    start_date: datetime,
    end_date: datetime,
    task_ids: list[str],
) -> str:
    """Create a proposed study plan without activating it (compatibility API)."""
    return create_plan_with_details(
        student_id=student_id,
        start_date=start_date,
        end_date=end_date,
        task_ids=task_ids,
    )["plan_id"]
