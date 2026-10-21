
"""Shared data models for CampusPilot."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Task:
    task_id: str
    student_id: str
    title: str
    due_at: Optional[datetime] = None
    estimated_minutes: int = 30
    priority: str = "medium"
    status: str = "pending"


@dataclass
class Deadline:
    deadline_id: str
    student_id: str
    title: str
    due_at: datetime
    deadline_type: str
    course: Optional[str] = None
    status: str = "open"


@dataclass
class ScheduleEvent:
    event_id: str
    student_id: str
    title: str
    start_at: datetime
    end_at: datetime
    event_type: str
    is_fixed: bool = True
