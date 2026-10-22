
"""SQLite database setup for CampusPilot."""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "campuspilot.db"


def get_db_path() -> Path:
    """Return the configured database path."""
    configured_path = os.getenv("CAMPUSPILOT_DB")
    if configured_path:
        if configured_path == ":memory:":
            return Path(":memory:")
        return Path(configured_path).expanduser().resolve()
    return DEFAULT_DB_PATH


@contextmanager
def get_connection():
    """Open and close a short-lived database connection."""
    db_path = get_db_path()

    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(str(db_path), timeout=5.0)
    connection.row_factory = sqlite3.Row

    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")

        if str(db_path) != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")

        yield connection
    finally:
        connection.close()


@contextmanager
def transaction(connection: sqlite3.Connection):
    """Commit on success and roll back on failure."""
    if connection.in_transaction:
        savepoint = "campuspilot_nested_transaction"
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            yield connection
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
    else:
        connection.execute("BEGIN")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    student_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    due_at TEXT,
    estimated_minutes INTEGER DEFAULT 30,
    priority TEXT DEFAULT 'medium',
    status TEXT DEFAULT 'pending',
    source TEXT DEFAULT 'manual',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deadline_id TEXT,
    FOREIGN KEY (student_id) REFERENCES students(student_id),
    FOREIGN KEY (deadline_id) REFERENCES deadlines(deadline_id)
);

CREATE TABLE IF NOT EXISTS deadlines (
    deadline_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    title TEXT NOT NULL,
    due_at TEXT NOT NULL,
    type TEXT NOT NULL,
    course TEXT,
    weight TEXT,
    status TEXT DEFAULT 'open',
    source TEXT DEFAULT 'manual',
    FOREIGN KEY (student_id) REFERENCES students(student_id)
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    title TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    location TEXT,
    is_fixed INTEGER DEFAULT 1,
    source TEXT DEFAULT 'manual',
    FOREIGN KEY (student_id) REFERENCES students(student_id)
);

CREATE TABLE IF NOT EXISTS preferences (
    student_id TEXT PRIMARY KEY,
    daily_start_time TEXT DEFAULT '09:00',
    daily_end_time TEXT DEFAULT '22:00',
    preferred_break_minutes INTEGER DEFAULT 15,
    max_focus_minutes INTEGER DEFAULT 90,
    stress_level TEXT DEFAULT 'okay',
    energy_level TEXT DEFAULT 'medium',
    FOREIGN KEY (student_id) REFERENCES students(student_id)
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'active', 'superseded', 'cancelled')),
    summary TEXT,
    FOREIGN KEY (student_id) REFERENCES students(student_id)
);

CREATE TABLE IF NOT EXISTS plan_items (
    plan_item_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    task_id TEXT,
    event_id TEXT,
    title TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    item_type TEXT NOT NULL,
    priority TEXT,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'scheduled', 'completed', 'skipped')),
    reason TEXT,
    FOREIGN KEY (plan_id) REFERENCES plans(plan_id)
);

CREATE TABLE IF NOT EXISTS checkins (
    checkin_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    related_event_id TEXT,
    related_task_id TEXT,
    occurred_at TEXT NOT NULL,
    completion_status TEXT,
    blocker TEXT,
    mood TEXT,
    energy TEXT,
    followup_created INTEGER DEFAULT 0,
    consent_given INTEGER DEFAULT 0,
    FOREIGN KEY (student_id) REFERENCES students(student_id)
);

CREATE TABLE IF NOT EXISTS tool_logs (
    log_id TEXT PRIMARY KEY,
    student_id TEXT,
    request_text TEXT,
    tool_name TEXT,
    arguments_json TEXT,
    result_json TEXT,
    success INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notices (
    notice_id TEXT PRIMARY KEY,
    student_id TEXT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    category TEXT,
    posted_at TEXT NOT NULL,
    is_read INTEGER DEFAULT 0,
    FOREIGN KEY (student_id) REFERENCES students(student_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_student
    ON tasks(student_id);
CREATE INDEX IF NOT EXISTS idx_tasks_student_due
    ON tasks(student_id, due_at);
CREATE INDEX IF NOT EXISTS idx_deadlines_student
    ON deadlines(student_id);
CREATE INDEX IF NOT EXISTS idx_events_student_start
    ON events(student_id, start_at);
CREATE INDEX IF NOT EXISTS idx_plan_items_plan
    ON plan_items(plan_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status
    ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_plans_status
    ON plans(status);
CREATE INDEX IF NOT EXISTS idx_plan_items_status
    ON plan_items(status);
"""


def init_db() -> None:
    """Create all CampusPilot tables and indexes safely."""
    with get_connection() as connection:
        with transaction(connection):
            connection.executescript(SCHEMA)
