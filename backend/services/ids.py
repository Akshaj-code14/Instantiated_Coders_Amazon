
"""ID generation helpers for CampusPilot."""

import re
import uuid

ALLOWED_PREFIXES = {
    "task",
    "plan",
    "event",
    "deadline",
    "item",
    "checkin",
    "notice",
    "log",
}

SUFFIX_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def new_id(prefix: str) -> str:
    """Generate a unique ID using an approved prefix."""
    if prefix not in ALLOWED_PREFIXES:
        raise ValueError(f"Unsupported ID prefix: {prefix}")

    return f"{prefix}_{uuid.uuid4().hex}"


def validate_id(value: str, expected_prefix: str | None = None) -> str:
    """Validate an ID and optionally check its prefix."""
    if not isinstance(value, str) or "_" not in value:
        raise ValueError("ID must contain a prefix and suffix.")

    prefix, suffix = value.split("_", 1)

    if prefix not in ALLOWED_PREFIXES:
        raise ValueError(f"Unsupported ID prefix: {prefix}")

    if expected_prefix is not None and prefix != expected_prefix:
        raise ValueError(
            f"Expected an ID beginning with '{expected_prefix}_'."
        )

    if not suffix or not SUFFIX_PATTERN.fullmatch(suffix):
        raise ValueError("ID suffix contains invalid characters.")

    return value
