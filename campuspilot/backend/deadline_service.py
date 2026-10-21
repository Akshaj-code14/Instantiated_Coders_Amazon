
"""Database operations for CampusPilot deadlines."""

from datetime import datetime, timedelta

from backend.database import get_connection, transaction
from backend.ids import new_id
from backend.timeutil import now_ist, require_aware, to_iso


def create_deadline(
    *,
    student_id: str,
    title: str,
    due_at: datetime,
    deadline_type: str,
    course: str | None = None,
    weight: str | None = None,
) -> str:
    """Create a deadline and return its ID."""
    if not student_id.strip():
        raise ValueError("student_id cannot be empty.")

    if not title.strip():
        raise ValueError("Deadline title cannot be empty.")

    require_aware(due_at, "due_at")

    if not deadline_type.strip():
        raise ValueError("deadline_type cannot be empty.")

    deadline_id = new_id("deadline")

    with get_connection() as connection:
        with transaction(connection):
            connection.execute(
                """
                INSERT INTO deadlines (
                    deadline_id, student_id, title, due_at,
                    type, course, weight, status, source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open', 'manual')
                """,
                (
                    deadline_id,
                    student_id,
                    title.strip(),
                    to_iso(due_at),
                    deadline_type.strip(),
                    course,
                    weight,
                ),
            )

    return deadline_id


def get_upcoming_deadlines(
    *,
    student_id: str,
    days_ahead: int,
) -> list[dict]:
    """Return open deadlines due within the requested number of days."""
    if not student_id.strip():
        raise ValueError("student_id cannot be empty.")

    if days_ahead < 0:
        raise ValueError("days_ahead cannot be negative.")

    now = now_ist()
    cutoff = now + timedelta(days=days_ahead)

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT deadline_id, student_id, title, due_at, type,
                   course, weight, status, source
            FROM deadlines
            WHERE student_id = ? AND status = 'open'
            """,
            (student_id,),
        ).fetchall()

    upcoming = []
    for row in rows:
        deadline = dict(row)
        due_at = datetime.fromisoformat(deadline["due_at"])

        if now <= due_at <= cutoff:
            upcoming.append(deadline)

    upcoming.sort(key=lambda item: datetime.fromisoformat(item["due_at"]))
    return upcoming
