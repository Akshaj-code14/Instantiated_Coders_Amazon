
"""Student check-in services for CampusPilot."""

from services.database import get_connection, transaction
from services.ids import new_id
from services.timeutil import now_ist, to_iso


VALID_COMPLETION_STATUSES = {
    "completed",
    "partially_completed",
    "not_completed",
    "skipped",
    # INTEGRATION: F6 (Phase 2b) -- the agent's frozen CompletionStatus uses
    # "partial" and "needs_help"; store them as-is instead of rejecting them.
    "partial",
    "needs_help",
}


def log_checkin(
    *,
    student_id: str,
    event_id: str | None,
    completion_status: str,
    blocker: str | None = None,
    mood: str | None = None,
    energy: int | None = None,
) -> str:
    """Record a student's check-in for a scheduled event.

    INTEGRATION: F6 (Phase 2b) -- ``event_id`` is optional so the UI can record
    a check-in when the student has no calendar event (A6); mood consent is
    recorded when a mood was actually submitted.
    """

    if not student_id.strip():
        raise ValueError("student_id cannot be empty.")

    if event_id is not None and not event_id.strip():
        raise ValueError("event_id cannot be empty when provided.")

    if completion_status not in VALID_COMPLETION_STATUSES:
        raise ValueError(
            "completion_status must be one of: "
            + ", ".join(sorted(VALID_COMPLETION_STATUSES))
        )

    if energy is not None and (
        not isinstance(energy, int) or isinstance(energy, bool)
        or not 1 <= energy <= 5
    ):
        raise ValueError("energy must be an integer between 1 and 5.")

    checkin_id = new_id("checkin")
    occurred_at = to_iso(now_ist())

    with get_connection() as connection:
        with transaction(connection):
            if event_id is not None:
                # Confirm the event belongs to this student.
                event = connection.execute(
                    """
                    SELECT event_id
                    FROM events
                    WHERE event_id = ? AND student_id = ?
                    """,
                    (event_id, student_id),
                ).fetchone()

                if event is None:
                    raise ValueError(
                        "The event was not found for this student."
                    )

            mood_value = mood.strip() if mood and mood.strip() else None
            connection.execute(
                """
                INSERT INTO checkins (
                    checkin_id, student_id, related_event_id,
                    occurred_at, completion_status, blocker,
                    mood, energy, followup_created, consent_given
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    checkin_id,
                    student_id,
                    event_id,
                    occurred_at,
                    completion_status,
                    blocker.strip() if blocker and blocker.strip() else None,
                    mood_value,
                    energy,
                    int(mood_value is not None),
                ),
            )

    return checkin_id
