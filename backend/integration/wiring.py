"""Thin wiring between the REST server, the agent and the backend.

One CampusPilotAgent exists per browser session. On a new session (including
after a server restart) pending/active plans are rebuilt from SQLite, and the
agent's ID provenance set is seeded with those database IDs so the confirm gate
still works after a refresh.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import services.campuspilot_backend as backend
from agent.config import load_config
from agent.controller import CampusPilotAgent
from agent.live_budget import budget
from agent.llm_client import GeminiLLMClient, LLMUnavailableError
from services.database import get_connection, init_db, transaction
from services.planner import detect_deadline_conflicts
from services.seed_data import DEMO_STUDENT_ID as SEEDED_STUDENT_ID
from services.seed_data import seed_demo
from services.timeutil import IST, now_ist, to_iso, to_ist
from integration.mappers import diff_between, plan_items_from_rows

logger = logging.getLogger(__name__)

STUDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
IDLE_TIMEOUT_SECONDS = 2 * 60 * 60
PLAN_LOOKAHEAD = timedelta(days=14)


class OfflineLLM:
    """Deterministic fallback: every call hands over to the rule-based router."""

    def __init__(self, reason: str = "GEMINI_API_KEY missing") -> None:
        self.reason = reason

    def generate(self, **_: Any):
        raise LLMUnavailableError(self.reason)


class ScopedBackend:
    """Forward to the backend module, adding ownership to update_task.

    The agent's frozen update_task tool has no student_id, so the wrapper adds
    the session's student ID. Every other call is forwarded untouched.
    """

    def __init__(self, student_id: str) -> None:
        self.student_id = student_id

    def __getattr__(self, name: str):
        return getattr(backend, name)

    def update_task(self, **kwargs: Any) -> dict:
        kwargs.setdefault("student_id", self.student_id)
        return backend.update_task(**kwargs)


def demo_student_id() -> str:
    configured = (os.getenv("DEMO_STUDENT_ID") or "").strip()
    if configured and STUDENT_RE.match(configured):
        return configured
    if configured:
        logger.warning("DEMO_STUDENT_ID is invalid; using student_demo")
    return "student_demo"


def llm_status() -> str:
    """FIX (deploy-ready, item 3): live | offline | capped, never the key."""
    if not load_config().gemini_api_key:
        return "offline"
    return "capped" if budget.remaining() <= 0 else "live"


def _data(envelope: Any) -> dict:
    if isinstance(envelope, dict) and envelope.get("success"):
        data = envelope.get("data")
        return data if isinstance(data, dict) else {}
    return {}


def _minutes(start_at: str, end_at: str) -> int:
    start = datetime.fromisoformat(start_at)
    end = datetime.fromisoformat(end_at)
    return int((end - start).total_seconds() // 60)


def _blocks(items: list[dict]) -> list[dict]:
    return [
        {
            "item_id": item.get("plan_item_id"),
            "task_id": item.get("task_id"),
            "title": item.get("title"),
            "start_at": item.get("start_at"),
            "end_at": item.get("end_at"),
            "minutes": _minutes(item["start_at"], item["end_at"]),
            "status": item.get("status"),
        }
        for item in items
    ]


def _working_items(items: list[dict]) -> list[dict]:
    return [
        item
        for item in items
        if item.get("status") in ("proposed", "scheduled")
    ]


def rehydrate_state(student_id: str) -> dict:
    """Rebuild the agent state from the database for a new browser session."""
    active = _data(backend.get_active_plan(student_id))
    pending = _data(backend.get_pending_plan(student_id))
    context = _data(backend.get_student_context(student_id=student_id))

    now = now_ist()
    deadlines = _data(
        backend.get_upcoming_deadlines(student_id=student_id, days_ahead=14)
    ).get("deadlines", [])
    events = _data(
        backend.get_schedule(
            student_id=student_id,
            start_date=now,
            end_date=now + PLAN_LOOKAHEAD,
        )
    ).get("events", [])

    active_plan = active.get("plan")
    active_items = active.get("items") or []
    pending_plan = pending.get("plan")
    pending_items = pending.get("items") or []

    seen: set[str] = set()
    for plan in (active_plan, pending_plan):
        if plan and plan.get("plan_id"):
            seen.add(plan["plan_id"])
    for item in list(active_items) + list(pending_items):
        for key in ("task_id", "event_id"):
            value = item.get(key)
            if isinstance(value, str) and value:
                seen.add(value)
    for task in context.get("tasks") or []:
        if task.get("task_id"):
            seen.add(task["task_id"])
    for task in deadlines:
        for key in ("id", "task_id", "deadline_id"):
            value = task.get(key)
            if isinstance(value, str) and value:
                seen.add(value)
    for event in events:
        if event.get("event_id"):
            seen.add(event["event_id"])

    pending_state = None
    if pending_plan:
        pending_state = {
            "plan_id": pending_plan["plan_id"],
            "summary": pending_plan.get("summary") or "Proposed plan",
            "diff": diff_between(active_items, pending_items),
            "kind": "replan_schedule" if active_plan else "create_plan",
            "created_at": pending_plan.get("created_at") or to_iso(now),
        }

    chosen_plan = pending_plan or active_plan
    chosen_items = pending_items if pending_plan else active_items
    last_plan = None
    if chosen_plan:
        working = _working_items(chosen_items)
        blocks = _blocks(working)
        last_plan = {
            "plan_id": chosen_plan["plan_id"],
            "total_minutes": sum(block["minutes"] for block in blocks),
            "blocks": blocks[:50],
            "task_ids": sorted(
                {
                    block["task_id"]
                    for block in blocks
                    if block.get("task_id")
                }
            ),
            "created_at": chosen_plan.get("created_at") or to_iso(now),
        }

    preferences = context.get("preferences") or {}
    student = context.get("student") or {}
    return {
        "pending": pending_state,
        "active_plan_id": active_plan.get("plan_id") if active_plan else None,
        "last_plan": last_plan,
        "seen_ids": sorted(seen),
        "cache": {
            "deadlines": deadlines,
            "events": events,
        },
        "context": {
            "student_name": student.get("name"),
            "prefs": preferences,
        },
    }


def build_agent(student_id: str | None = None) -> CampusPilotAgent:
    student_id = student_id or demo_student_id()
    config = load_config()
    llm = GeminiLLMClient(config) if config.gemini_api_key else OfflineLLM()
    return CampusPilotAgent(
        student_id,
        ScopedBackend(student_id),
        llm,
        state=rehydrate_state(student_id),
        config=config,
    )


@dataclass
class Session:
    student_id: str
    agent: CampusPilotAgent
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_seen: float = field(default_factory=time.monotonic)


class SessionRegistry:
    """Opaque cookie -> one agent per browser, with idle cleanup."""

    def __init__(self, student_id: str | None = None) -> None:
        self.student_id = student_id or demo_student_id()
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()

    def _cleanup(self) -> None:
        cutoff = time.monotonic() - IDLE_TIMEOUT_SECONDS
        stale = [
            key for key, session in self._sessions.items()
            if session.last_seen < cutoff
        ]
        for key in stale:
            self._sessions.pop(key, None)

    def get_or_create(self, session_id: str | None) -> tuple[str, Session]:
        with self._lock:
            self._cleanup()
            if session_id and session_id in self._sessions:
                session = self._sessions[session_id]
                session.last_seen = time.monotonic()
                return session_id, session

            # Reuse an unknown cookie after a restart: the agent is rebuilt from
            # SQLite, so the browser keeps its identity and its saved plans.
            new_id = session_id or secrets.token_urlsafe(32)
            session = Session(
                student_id=self.student_id,
                agent=build_agent(self.student_id),
            )
            self._sessions[new_id] = session
            return new_id, session

    def reset(self) -> None:
        with self._lock:
            self._sessions.clear()

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)


def startup() -> None:
    """Create tables and seed the demo student only when the DB is empty."""
    init_db()
    summary = seed_demo()
    if not summary.get("seeded"):
        logger.info("seed skipped: %s", summary.get("reason"))
    target = demo_student_id()
    if target != SEEDED_STUDENT_ID:
        with get_connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM students WHERE student_id = ?", (target,)
            ).fetchone()
        if exists is None:
            logger.warning(
                "DEMO_STUDENT_ID=%s has no student row; the demo expects %s",
                target, SEEDED_STUDENT_ID,
            )


def reset_demo_data() -> dict:
    """FIX (deploy-ready, item 5): wipe the demo student and seed it again.

    Every visitor shares one demo student, so a reset is global by design.
    Only rows owned by the demo student are removed; the schema is kept and
    ``seed_demo`` is idempotent and relative to the current IST time.
    """
    targets = {SEEDED_STUDENT_ID, demo_student_id()}
    with get_connection() as connection:
        with transaction(connection):
            for target in targets:
                connection.execute(
                    """
                    DELETE FROM plan_items
                    WHERE plan_id IN (
                        SELECT plan_id FROM plans WHERE student_id = ?
                    )
                    """,
                    (target,),
                )
                for table in (
                    "plans",
                    "checkins",
                    "tasks",
                    "deadlines",
                    "events",
                    "preferences",
                    "tool_logs",
                ):
                    connection.execute(
                        f"DELETE FROM {table} WHERE student_id = ?", (target,)
                    )
                # The seeded notices are campus-wide (student_id NULL), so a
                # student-scoped delete would leave them behind and the reseed
                # would hit the notice_id primary key.
                connection.execute(
                    """
                    DELETE FROM notices
                    WHERE student_id = ? OR notice_id LIKE 'notice_demo_%'
                    """,
                    (target,),
                )
                connection.execute(
                    "DELETE FROM students WHERE student_id = ?", (target,)
                )
    summary = seed_demo()
    logger.info("demo reset: %s", summary.get("reason", "seeded"))
    return {"seeded": bool(summary.get("seeded")), "student_id": SEEDED_STUDENT_ID}


def cancel_pending_plan(student_id: str, plan_id: str) -> bool:
    """Mark the pending proposal cancelled so /state and restarts agree."""
    with get_connection() as connection:
        with transaction(connection):
            cursor = connection.execute(
                """
                UPDATE plans
                SET status = 'cancelled'
                WHERE plan_id = ? AND student_id = ? AND status = 'proposed'
                """,
                (plan_id, student_id),
            )
    return cursor.rowcount > 0


def update_preferences(student_id: str, **fields: object) -> dict:
    """Persist the editable study preferences. No schema change: the
    table already holds these columns, it just had no write path."""
    allowed = {
        "daily_start_time",
        "daily_end_time",
        "preferred_break_minutes",
        "max_focus_minutes",
    }
    updates = {key: value for key, value in fields.items() if key in allowed and value is not None}
    if not updates:
        return {}
    assignments = ", ".join(f"{key} = ?" for key in updates)
    values = list(updates.values()) + [student_id]
    with get_connection() as connection:
        with transaction(connection):
            connection.execute(
                f"UPDATE preferences SET {assignments} WHERE student_id = ?",
                values,
            )
        row = connection.execute(
            """
            SELECT daily_start_time, daily_end_time,
                   preferred_break_minutes, max_focus_minutes
            FROM preferences
            WHERE student_id = ?
            """,
            (student_id,),
        ).fetchone()
    return dict(row) if row else {}


def owned_task(student_id: str, task_id: str) -> bool:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT student_id FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    return bool(row and row["student_id"] == student_id)


def task_due_at(student_id: str, task_id: str) -> str | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT due_at FROM tasks WHERE task_id = ? AND student_id = ?",
            (task_id, student_id),
        ).fetchone()
    return row["due_at"] if row else None


def most_recent_event(student_id: str) -> str | None:
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT event_id
            FROM events
            WHERE student_id = ? AND end_at <= ?
            ORDER BY end_at DESC
            LIMIT 1
            """,
            (student_id, to_iso(now_ist())),
        ).fetchone()
    return row["event_id"] if row else None


def build_state(student_id: str) -> dict:
    """Read-only snapshot for GET /state."""
    now = now_ist()
    with get_connection() as connection:
        student = connection.execute(
            "SELECT student_id, name FROM students WHERE student_id = ?",
            (student_id,),
        ).fetchone()
        preferences = connection.execute(
            "SELECT * FROM preferences WHERE student_id = ?", (student_id,)
        ).fetchone()
        tasks = connection.execute(
            """
            SELECT task_id, title, due_at, estimated_minutes, priority,
                   status, deadline_id
            FROM tasks
            WHERE student_id = ?
            ORDER BY
              CASE status WHEN 'completed' THEN 1 ELSE 0 END,
              due_at IS NULL, due_at,
              CASE priority
                WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3
              END,
              task_id
            """,
            (student_id,),
        ).fetchall()
        active_plan = connection.execute(
            """
            SELECT * FROM plans
            WHERE student_id = ? AND status = 'active'
            ORDER BY created_at DESC LIMIT 1
            """,
            (student_id,),
        ).fetchone()
        pending_plan = connection.execute(
            """
            SELECT * FROM plans
            WHERE student_id = ? AND status = 'proposed'
            ORDER BY created_at DESC LIMIT 1
            """,
            (student_id,),
        ).fetchone()

        active_items = []
        if active_plan:
            active_items = connection.execute(
                """
                SELECT * FROM plan_items
                WHERE plan_id = ? ORDER BY start_at
                """,
                (active_plan["plan_id"],),
            ).fetchall()
        pending_items = []
        if pending_plan:
            pending_items = connection.execute(
                """
                SELECT * FROM plan_items
                WHERE plan_id = ? ORDER BY start_at
                """,
                (pending_plan["plan_id"],),
            ).fetchall()

        events = connection.execute(
            """
            SELECT event_id, title, start_at, end_at, event_type,
                   location, is_fixed
            FROM events
            WHERE student_id = ? AND start_at < ? AND end_at > ?
            ORDER BY start_at
            """,
            (student_id, to_iso(now + timedelta(days=3)),
             to_iso(now - timedelta(hours=6))),
        ).fetchall()

        checkin = connection.execute(
            """
            SELECT checkin_id, completion_status, mood, occurred_at,
                   related_event_id
            FROM checkins
            WHERE student_id = ?
            ORDER BY occurred_at DESC LIMIT 1
            """,
            (student_id,),
        ).fetchone()

        notice_total = connection.execute(
            "SELECT COUNT(*) AS n FROM notices"
        ).fetchone()["n"]
        notice_unread = connection.execute(
            "SELECT COUNT(*) AS n FROM notices WHERE is_read = 0"
        ).fetchone()["n"]

    name = student["name"] if student else "Student"
    initials = "".join(part[0] for part in name.split()[:2]).upper() or "CP"
    prefs = dict(preferences) if preferences else {}

    slot_by_task: dict[str, dict] = {}
    for item in active_items:
        if item["status"] != "scheduled" or not item["task_id"]:
            continue
        slot_by_task.setdefault(
            item["task_id"],
            {
                "start_at": item["start_at"],
                "end_at": item["end_at"],
                "plan_id": active_plan["plan_id"],
            },
        )

    task_payload = []
    completed = 0
    for task in tasks:
        status = task["status"]
        if status == "completed":
            completed += 1
        task_payload.append(
            {
                "id": task["task_id"],
                "task_id": task["task_id"],
                "title": task["title"],
                "due_at": task["due_at"],
                "estimated_minutes": int(task["estimated_minutes"] or 30),
                "priority": task["priority"] or "medium",
                "status": status,
                "done": status == "completed",
                "slot": slot_by_task.get(task["task_id"]),
            }
        )

    timeline: list[dict] = []
    for event in events:
        timeline.append(
            {
                "id": event["event_id"],
                "title": event["title"],
                "start_at": event["start_at"],
                "end_at": event["end_at"],
                "kind": event["event_type"],
                "status": "completed"
                if datetime.fromisoformat(event["end_at"]) < now
                else "fixed",
                "location": event["location"],
                "is_fixed": bool(event["is_fixed"]),
            }
        )
    for item in active_items:
        timeline.append(
            {
                "id": item["plan_item_id"],
                "title": item["title"],
                "start_at": item["start_at"],
                "end_at": item["end_at"],
                "kind": "study",
                "status": item["status"],
                "location": None,
                "is_fixed": False,
            }
        )
    timeline.sort(key=lambda item: item["start_at"])

    # Conflicts are a property of the student's real deadlines and fixed
    # events, not of which blocks happened to be scheduled. Checking all tasks
    # keeps an unscheduled-but-due task (the DBMS lab here) visible.
    reference_plan = pending_plan or active_plan
    if reference_plan:
        conflict_start = to_ist(
            datetime.fromisoformat(reference_plan["valid_from"])
        )
        conflict_end = to_ist(
            datetime.fromisoformat(reference_plan["valid_to"])
        )
    else:
        conflict_start = now
        conflict_end = now + timedelta(days=14)
    conflict_rows = detect_deadline_conflicts(
        student_id=student_id,
        start_date=conflict_start,
        end_date=conflict_end,
        now=now,
    )
    conflict_notes = [item["note"] for item in conflict_rows]

    pending_payload = None
    if pending_plan:
        pending_items_dicts = [dict(item) for item in pending_items]
        pending_payload = {
            "plan_id": pending_plan["plan_id"],
            "summary": pending_plan["summary"] or "Proposed plan",
            "diff": diff_between(
                [dict(item) for item in active_items],
                pending_items_dicts,
            ),
            "items": plan_items_from_rows(pending_items_dicts),
            "warnings": conflict_notes,
            "conflicts": conflict_notes,
        }

    return {
        "profile": {
            "name": name,
            "initials": initials,
            "subtitle": "CampusPilot demo student",
        },
        "preferences": {
            "daily_start_time": prefs.get("daily_start_time", "09:00"),
            "daily_end_time": prefs.get("daily_end_time", "22:00"),
            "preferred_break_minutes": int(
                prefs.get("preferred_break_minutes", 15) or 15
            ),
            "max_focus_minutes": int(prefs.get("max_focus_minutes", 90) or 90),
            "stress_level": prefs.get("stress_level", "okay"),
            "energy_level": prefs.get("energy_level", "medium"),
        },
        "tasks": task_payload,
        "timeline": timeline,
        "active_plan": (
            {
                "plan_id": active_plan["plan_id"],
                "summary": active_plan["summary"],
                "status": active_plan["status"],
                "valid_from": active_plan["valid_from"],
                "valid_to": active_plan["valid_to"],
                "items": plan_items_from_rows(
                    [dict(item) for item in active_items]
                ),
            }
            if active_plan
            else None
        ),
        "pending_plan": pending_payload,
        "counts": {
            "total": len(task_payload),
            "completed": completed,
            "percent": int(round(completed / len(task_payload) * 100))
            if task_payload
            else 0,
        },
        "checkin": dict(checkin) if checkin else None,
        "notices": {"total": notice_total, "unread": notice_unread},
        "date_label": now.strftime("%A, %b %d"),
        "llm": llm_status(),
        "conflicts": conflict_notes,
    }


def pending_plan_data(student_id: str) -> dict:
    data = _data(backend.get_pending_plan(student_id))
    return {
        "plan": data.get("plan"),
        "items": data.get("items") or [],
    }


def pending_conflicts(student_id: str) -> list[dict]:
    data = pending_plan_data(student_id)
    plan = data.get("plan")
    if not plan:
        return []
    return detect_deadline_conflicts(
        student_id=student_id,
        start_date=to_ist(datetime.fromisoformat(plan["valid_from"])),
        end_date=to_ist(datetime.fromisoformat(plan["valid_to"])),
        now=now_ist(),
    )
