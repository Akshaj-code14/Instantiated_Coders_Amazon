
"""Date and time helpers for CampusPilot."""

from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    """Return the current time in Indian Standard Time."""
    return datetime.now(IST)


def require_aware(value: datetime, field_name: str = "datetime") -> datetime:
    """Reject datetimes that do not specify a timezone."""
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime object.")

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information.")

    return value


def to_iso(value: datetime, field_name: str = "datetime") -> str:
    """Validate a datetime and return its ISO 8601 representation."""
    return require_aware(value, field_name).isoformat()


def from_iso(value: str) -> datetime:
    """Parse an ISO 8601 string and ensure the result is timezone-aware."""
    parsed = datetime.fromisoformat(value)
    return require_aware(parsed, "value")


def to_ist(value: datetime) -> datetime:
    """Convert a timezone-aware datetime to Indian Standard Time."""
    return require_aware(value).astimezone(IST)
