
"""Database operations for CampusPilot schedules."""

from datetime import datetime

from services.database import get_connection, transaction
from services.ids import new_id
from services.timeutil import require_aware, to_iso


def create_event(
    *,
    student_id: str,
    title: str,
    start_at: datetime,
    end_at: datetime,
    event_type: str,
    location: str | None = None,
    is_fixed: bool = True,
) -> str:
    """Create a calendar event and return its ID."""
    if not student_id.strip():
        raise ValueError("student_id cannot be empty.")

    if not title.strip():
        raise ValueError("Event title cannot be empty.")

    require_aware(start_at, "start_at")
    require_aware(end_at, "end_at")

    if end_at <= start_at:
        raise ValueError("end_at must be after start_at.")

    if not event_type.strip():
        raise ValueError("event_type cannot be empty.")

    event_id = new_id("event")

    with get_connection() as connection:
        with transaction(connection):
            connection.execute(
                """
                INSERT INTO events (
                    event_id, student_id, title, start_at, end_at,
                    event_type, location, is_fixed, source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual')
                """,
                (
                    event_id,
                    student_id,
                    title.strip(),
                    to_iso(start_at),
                    to_iso(end_at),
                    event_type.strip(),
                    location,
                    int(is_fixed),
                ),
            )

    return event_id


def get_schedule(
    *,
    student_id: str,
    start_date: datetime,
    end_date: datetime,
) -> list[dict]:
    """Return events overlapping the requested time interval."""
    if not student_id.strip():
        raise ValueError("student_id cannot be empty.")

    require_aware(start_date, "start_date")
    require_aware(end_date, "end_date")

    if end_date <= start_date:
        raise ValueError("end_date must be after start_date.")

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT event_id, student_id, title, start_at, end_at,
                   event_type, location, is_fixed, source
            FROM events
            WHERE student_id = ?
              AND start_at < ?
              AND end_at > ?
            ORDER BY start_at
            """,
            (
                student_id,
                to_iso(end_date),
                to_iso(start_date),
            ),
        ).fetchall()

    return [dict(row) for row in rows]
