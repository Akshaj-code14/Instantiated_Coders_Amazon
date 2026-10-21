
"""Database operations for CampusPilot tasks."""

from datetime import datetime
from typing import Optional

from backend.database import get_connection, transaction
from backend.ids import new_id
from backend.timeutil import now_ist, require_aware, to_iso


def create_task(
    *,
    student_id: str,
    title: str,
    due_at: Optional[datetime] = None,
    estimated_minutes: Optional[int] = None,
    priority: str = "medium",
) -> str:
    """Create a task and return its ID."""
    if not student_id.strip():
        raise ValueError("student_id cannot be empty.")

    if not title.strip():
        raise ValueError("Task title cannot be empty.")

    if due_at is not None:
        require_aware(due_at, "due_at")

    minutes = 30 if estimated_minutes is None else estimated_minutes
    if minutes <= 0:
        raise ValueError("estimated_minutes must be positive.")

    if priority not in {"low", "medium", "high"}:
        raise ValueError("priority must be low, medium, or high.")

    task_id = new_id("task")
    now = to_iso(now_ist())

    with get_connection() as connection:
        with transaction(connection):
            connection.execute(
                """
                INSERT INTO tasks (
                    task_id, student_id, title, due_at,
                    estimated_minutes, priority, status,
                    source, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'pending', 'manual', ?, ?)
                """,
                (
                    task_id,
                    student_id,
                    title.strip(),
                    to_iso(due_at) if due_at else None,
                    minutes,
                    priority,
                    now,
                    now,
                ),
            )

    return task_id


def get_all_tasks(student_id: str) -> list[dict]:
    """Return all tasks belonging to a student."""
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT task_id, student_id, title, description, due_at,
                   estimated_minutes, priority, status, source,
                   created_at, updated_at, deadline_id
            FROM tasks
            WHERE student_id = ?
            ORDER BY due_at IS NULL, due_at, created_at
            """,
            (student_id,),
        ).fetchall()

    return [dict(row) for row in rows]


def update_task(
    *,
    task_id: str,
    status: Optional[str] = None,
    due_at: Optional[datetime] = None,
    estimated_minutes: Optional[int] = None,
    student_id: Optional[str] = None,
) -> bool:
    """Update provided task fields without changing unspecified fields.

    INTEGRATION: F6 (Phase 2b) -- ``postponed`` is part of the agent's frozen
    TaskStatus but was rejected here, and there was no ownership scope. The
    optional ``student_id`` is supplied by the server wrapper; the agent's
    frozen tool call does not include it.
    """
    if status is None and due_at is None and estimated_minutes is None:
        raise ValueError("At least one task field must be provided.")

    allowed_statuses = {
        "pending", "in_progress", "completed", "postponed", "cancelled",
    }
    if status is not None and status not in allowed_statuses:
        raise ValueError("Unsupported task status.")

    if due_at is not None:
        require_aware(due_at, "due_at")

    if estimated_minutes is not None and estimated_minutes <= 0:
        raise ValueError("estimated_minutes must be positive.")

    updates = []
    values = []

    if status is not None:
        updates.append("status = ?")
        values.append(status)

    if due_at is not None:
        updates.append("due_at = ?")
        values.append(to_iso(due_at))

    if estimated_minutes is not None:
        updates.append("estimated_minutes = ?")
        values.append(estimated_minutes)

    updates.append("updated_at = ?")
    values.append(to_iso(now_ist()))
    values.append(task_id)

    where = "task_id = ?"
    if student_id is not None:
        where += " AND student_id = ?"
        values.append(student_id)

    with get_connection() as connection:
        with transaction(connection):
            cursor = connection.execute(
                f"UPDATE tasks SET {', '.join(updates)} WHERE {where}",
                values,
            )

            # INTEGRATION: F4 (Phase 2b) -- keep plan items in step with the
            # task so a replan can preserve what was actually completed.
            if cursor.rowcount > 0 and status is not None:
                owner = connection.execute(
                    "SELECT student_id FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if owner is not None:
                    if status == "completed":
                        connection.execute(
                            """
                            UPDATE plan_items
                            SET status = 'completed'
                            WHERE task_id = ?
                              AND plan_id IN (
                                  SELECT plan_id FROM plans
                                  WHERE student_id = ? AND status = 'active'
                              )
                            """,
                            (task_id, owner["student_id"]),
                        )
                    elif status in ("pending", "in_progress", "postponed"):
                        connection.execute(
                            """
                            UPDATE plan_items
                            SET status = 'scheduled'
                            WHERE task_id = ? AND status = 'completed'
                              AND plan_id IN (
                                  SELECT plan_id FROM plans
                                  WHERE student_id = ? AND status = 'active'
                              )
                            """,
                            (task_id, owner["student_id"]),
                        )

    return cursor.rowcount > 0
