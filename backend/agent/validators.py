"""Validation helpers shared by the controller, registry and fallback router.

Two jobs:

1. **Dates** — only timezone-aware ISO 8601 timestamps are accepted, ranges are
   capped, and everything is normalised to IST.
2. **IDs** — an ID is only usable if it has the backend's prefix shape *and*
   already appeared in a tool result during this session. That is what stops
   the model from inventing ``plan_deadbeef``.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable

from agent.config import IST, MAX_RANGE_DAYS
from agent.schemas import (
    ID_PREFIXES,
    STUDENT_ID_PATTERN,
    ToolName,
    id_regex,
)

_WORD_NUMBERS: dict[str, float] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
}

_ID_ANY = re.compile(r"^(plan|task|event|deadline)_[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
_STUDENT_RE = re.compile(STUDENT_ID_PATTERN)
_NUMBER_PATTERN = r"(?:\d+(?:\.\d+)?|an?|half|" + "|".join(_WORD_NUMBERS) + r")"


class ToolValidationError(Exception):
    """A validation failure that is safe and useful to show to the model."""

    def __init__(self, field: str, friendly_message: str) -> None:
        super().__init__(friendly_message)
        self.field = field
        self.friendly_message = friendly_message


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def parse_aware_datetime(value: str | datetime, field: str = "date") -> datetime:
    """Parse an ISO 8601 timestamp with an offset, normalised to IST.

    Naive datetimes are rejected: we never assume the student's timezone.
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ToolValidationError(
                field,
                f"I couldn't read {field} as a date. Please give an exact date "
                "like 2026-10-21T18:00:00+05:30 (or say 'tomorrow evening').",
            ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ToolValidationError(
            field,
            f"{field} needs a timezone. Which date and time did you mean, "
            "in India Standard Time?",
        )
    return parsed.astimezone(IST)


def validate_date_window(
    start: datetime,
    end: datetime,
    *,
    max_days: int = MAX_RANGE_DAYS,
) -> tuple[datetime, datetime]:
    """Reject inverted or oversized ranges; return them normalised to IST."""
    begin = parse_aware_datetime(start, "start_date")
    finish = parse_aware_datetime(end, "end_date")
    if finish < begin:
        raise ToolValidationError(
            "end_date", "The end date is before the start date. Which range did you mean?"
        )
    if finish - begin > timedelta(days=max_days):
        raise ToolValidationError(
            "end_date",
            f"That range is too wide. I can plan up to {max_days} days at a time — "
            "which shorter range should I use?",
        )
    return begin, finish


def to_iso(value: datetime) -> str:
    return value.astimezone(IST).isoformat(timespec="minutes")


def day_bounds(day: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, time(0, 0), IST),
        datetime.combine(day, time(23, 59, 59), IST),
    )


def resolve_window_phrase(text: str, now: datetime) -> tuple[datetime, datetime] | None:
    """Resolve simple phrases ("today", "tomorrow", "next 3 days") deterministically.

    Used by the fallback router when the LLM is unavailable; never used to
    override an explicit date the model provided through a tool call.
    """
    current = now.astimezone(IST)
    lowered = text.lower()
    today = current.date()

    if re.search(r"\btoday\b", lowered):
        return day_bounds(today)
    if re.search(r"\btomorrow\b", lowered):
        return day_bounds(today + timedelta(days=1))
    if re.search(r"\bthis week\b", lowered):
        return (current, datetime.combine(today + timedelta(days=6), time(22, 0), IST))

    match = re.search(
        r"\bnext\s+(?P<count>\d{1,2}|an?|one|two|three|four|five|six|seven)\s+days?\b",
        lowered,
    )
    if match:
        count = _parse_number(match.group("count"))
        if count is None or not 1 <= int(count) <= MAX_RANGE_DAYS:
            return None
        return (current, current + timedelta(days=int(count)))
    return None


def _parse_number(token: str) -> float | None:
    token = token.strip().lower()
    if token in ("a", "an"):
        return 1.0
    if token == "half":
        return 0.5
    if token in _WORD_NUMBERS:
        return float(_WORD_NUMBERS[token])
    try:
        return float(token)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Durations ("I lost two hours", "90 minutes", "half an hour")
# ---------------------------------------------------------------------------

# `[\s-]*` also accepts the hyphenated forms people actually type
# ("15-min break", "2-hour slot").
_HOUR_RE = re.compile(
    rf"(?P<num>{_NUMBER_PATTERN})[\s-]*(?:and\s+a\s+half\s+)?(?:hours?|hrs?|h)\b"
)
_MINUTE_RE = re.compile(
    rf"(?P<num>{_NUMBER_PATTERN})[\s-]*(?:minutes?|mins?|m)\b"
)
_HALF_HOUR_RE = re.compile(r"\bhalf\s+an?\s+hour\b|\bhalf\s+hour\b")


def parse_duration_minutes(text: str) -> int | None:
    """Return a duration in minutes, or ``None`` when the text is ambiguous.

    Ambiguous means: more than one distinct duration is mentioned in a way that
    could not be a single "2 hours 30 minutes" style duration, or a contrast
    word splits two durations ("I lost two hours but have 90 minutes").
    """
    lowered = text.lower()
    if not lowered.strip():
        return None

    if re.search(r"\b(but|however|instead|although)\b", lowered):
        mentions = len(_HOUR_RE.findall(lowered)) + len(_MINUTE_RE.findall(lowered))
        if mentions > 1:
            return None

    total = 0.0
    matched = False

    if _HALF_HOUR_RE.search(lowered):
        total += 30
        matched = True
        lowered = _HALF_HOUR_RE.sub(" ", lowered)

    minute_matches = list(_MINUTE_RE.finditer(lowered))
    for match in minute_matches:
        number = _parse_number(match.group("num"))
        if number is None:
            continue
        total += number
        matched = True
    lowered = _MINUTE_RE.sub(" ", lowered)

    for match in _HOUR_RE.finditer(lowered):
        number = _parse_number(match.group("num"))
        if number is None:
            continue
        hours = number
        if re.search(r"and\s+a\s+half", match.group(0)):
            hours += 0.5
        total += hours * 60
        matched = True

    if not matched:
        return None
    minutes = int(total)
    if minutes <= 0 or minutes > 24 * 60:
        return None
    return minutes


def parse_days_ahead(text: str) -> int | None:
    """Interpret "next two days" / "in 5 days" for the fallback router."""
    lowered = text.lower()
    match = re.search(
        rf"\bnext\s+(?P<count>{_NUMBER_PATTERN})\s+days?\b", lowered
    )
    if match:
        number = _parse_number(match.group("count"))
        if number is None or not 1 <= number <= 30:
            return None
        return int(number)
    match = re.search(r"\b(?:in|within)\s+(?P<count>\d{1,2})\s+days?\b", lowered)
    if match:
        number = int(match.group("count"))
        return number if 1 <= number <= 30 else None
    return None


# ---------------------------------------------------------------------------
# IDs
# ---------------------------------------------------------------------------


def validate_student_id(student_id: str) -> str:
    if not student_id or not _STUDENT_RE.match(student_id):
        raise ToolValidationError(
            "student_id", "Invalid student session. Please sign in again."
        )
    return student_id


def collect_ids(payload: Any) -> set[str]:
    """Recursively collect backend-shaped IDs from any tool result payload."""
    found: set[str] = set()

    def walk(node: Any, key: str | None) -> None:
        if isinstance(node, dict):
            for child_key, child in node.items():
                walk(child, child_key)
        elif isinstance(node, list):
            for item in node:
                walk(item, key)
        elif isinstance(node, str) and key is not None:
            if (key == "id" or key.endswith("_id")) and _ID_ANY.match(node):
                found.add(node)

    walk(payload, None)
    return found


class SeenIds:
    """IDs observed so far in this session; the only IDs tools may use."""

    def __init__(self, initial: Iterable[str] | None = None) -> None:
        self._ids: set[str] = set(initial or ())

    def observe(self, payload: Any) -> set[str]:
        new_ids = collect_ids(payload)
        self._ids |= new_ids
        return new_ids

    def __contains__(self, value: object) -> bool:
        return isinstance(value, str) and value in self._ids

    def add(self, value: str) -> None:
        if _ID_ANY.match(value):
            self._ids.add(value)

    def of_prefix(self, field: str) -> list[str]:
        prefix = ID_PREFIXES[field] + "_"
        return sorted(value for value in self._ids if value.startswith(prefix))

    def to_list(self) -> list[str]:
        return sorted(self._ids)

    def __len__(self) -> int:
        return len(self._ids)


def validate_known_id(field: str, value: str, seen: SeenIds) -> str:
    """Reject an ID the model made up (or saw in a previous session)."""
    if not id_regex(field).match(value):
        raise ToolValidationError(
            field, f"{value!r} is not a valid {field}. Use an ID from a tool result."
        )
    if value not in seen:
        raise ToolValidationError(
            field,
            f"I don't have any {field} called {value!r} in this session. "
            "Use an ID returned by an earlier tool call, or list the data again first.",
        )
    return value


def validate_known_ids(field: str, values: Iterable[str], seen: SeenIds) -> list[str]:
    return [validate_known_id(field, value, seen) for value in values]


def tool_can_accept_student_id(tool: ToolName) -> bool:
    from agent.schemas import ARG_MODELS

    return "student_id" in ARG_MODELS[tool].model_fields
