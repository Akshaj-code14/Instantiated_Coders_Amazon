"""Mappers between the agent/backend shapes and the UI JSON contract."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

TOOL_SUMMARIES = {
    "get_student_context": "Read profile and preferences",
    "get_upcoming_deadlines": "Checked deadlines",
    "get_schedule": "Checked your schedule",
    "find_free_slots": "Found free time",
    "create_plan": "Created plan proposal",
    "save_plan": "Saved plan",
    "update_task": "Updated task",
    "create_task": "Created follow-up task",
    "replan_schedule": "Reworked the plan",
    "search_notices": "Searched notices",
    "log_checkin": "Recorded check-in",
    "get_wellbeing_support": "Shared support resources",
}

FAILURE_SUMMARIES = {
    "get_student_context": "Could not read the profile",
    "get_upcoming_deadlines": "Could not read deadlines",
    "get_schedule": "Could not read the schedule",
    "find_free_slots": "Could not read free time",
    "create_plan": "Could not create a plan",
    "save_plan": "Plan was not saved",
    "update_task": "Could not update the task",
    "create_task": "Could not create the task",
    "replan_schedule": "Could not rework the plan",
    "search_notices": "Could not search notices",
    "log_checkin": "Could not record the check-in",
    "get_wellbeing_support": "Could not load support resources",
}

WARN_ON_CONFLICT = {"find_free_slots", "create_plan", "replan_schedule"}


def _minutes(start_at: str, end_at: str) -> int:
    start = datetime.fromisoformat(start_at)
    end = datetime.fromisoformat(end_at)
    return int((end - start).total_seconds() // 60)


def map_tool_calls(traces: Iterable[Any], *, conflicts: int = 0) -> list[dict]:
    """Turn ToolCallTrace objects into the UI's chips/reasoning entries."""
    calls: list[dict] = []
    for trace in traces:
        name = getattr(trace, "name", "unknown_tool")
        success = bool(getattr(trace, "success", False))
        reason = getattr(trace, "reason", None) or ""
        warn = not success

        if name == "save_plan" and "confirmation gate" in reason:
            summary = "Waiting for your confirmation"
            warn = True
        elif not success:
            summary = FAILURE_SUMMARIES.get(name, "Action failed")
        else:
            summary = TOOL_SUMMARIES.get(name, name.replace("_", " ").title())

        if success and name in WARN_ON_CONFLICT and conflicts:
            warn = True
            summary = f"{summary} ({conflicts} conflict{'s' if conflicts != 1 else ''})"

        calls.append(
            {
                "name": name,
                "summary": summary,
                "status": "warn" if warn else "ok",
            }
        )
    return calls


def plan_item_from_row(row: Mapping[str, Any]) -> dict:
    start_at = row["start_at"]
    end_at = row["end_at"]
    return {
        "item_id": row.get("plan_item_id") or row.get("item_id"),
        "task_id": row.get("task_id"),
        "title": row.get("title") or "Study block",
        "start_at": start_at,
        "end_at": end_at,
        "minutes": _minutes(start_at, end_at),
        "status": row.get("status") or "proposed",
        "reason": row.get("reason"),
    }


def plan_items_from_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict]:
    return [plan_item_from_row(row) for row in rows]


def _entry(
    raw: Mapping[str, Any],
    *,
    default_reason: str,
) -> dict:
    from_start = raw.get("from_start")
    from_end = raw.get("from_end")
    to_start = raw.get("to_start")
    to_end = raw.get("to_end")

    source = raw.get("from") or {}
    target = raw.get("to") or {}
    if isinstance(source, Mapping):
        from_start = from_start or source.get("start_at")
        from_end = from_end or source.get("end_at")
    if isinstance(target, Mapping):
        to_start = to_start or target.get("start_at")
        to_end = to_end or target.get("end_at")

    return {
        "task_id": raw.get("task_id"),
        "title": raw.get("title") or "Study block",
        "from_start": from_start,
        "from_end": from_end,
        "to_start": to_start,
        "to_end": to_end,
        "reason": raw.get("reason") or default_reason,
    }


def normalize_diff(diff: Any, items: list[dict]) -> dict:
    """Normalise the agent's diff dict; derive additions for a new plan."""
    result = {"added": [], "moved": [], "removed": [], "unchanged": []}
    if isinstance(diff, Mapping):
        for key in result:
            values = diff.get(key) or []
            if isinstance(values, list):
                result[key] = [
                    _entry(value, default_reason=key)
                    for value in values
                    if isinstance(value, Mapping)
                ]

    if not any(result.values()) and items:
        result["added"] = [
            {
                "task_id": item.get("task_id"),
                "title": item.get("title") or "Study block",
                "from_start": None,
                "from_end": None,
                "to_start": item["start_at"],
                "to_end": item["end_at"],
                "reason": "newly scheduled in this plan",
            }
            for item in items
            if item.get("status") != "completed"
        ]

    return result


def diff_between(
    active_items: Iterable[Mapping[str, Any]],
    pending_items: Iterable[Mapping[str, Any]],
) -> dict:
    """Build the review-modal diff after a restart, from database rows."""
    old_by_task: dict[str, list[Mapping[str, Any]]] = {}
    new_by_task: dict[str, list[Mapping[str, Any]]] = {}

    for item in active_items:
        if item.get("task_id"):
            old_by_task.setdefault(item["task_id"], []).append(item)
    for item in pending_items:
        if item.get("task_id"):
            new_by_task.setdefault(item["task_id"], []).append(item)

    result = {"added": [], "moved": [], "removed": [], "unchanged": []}

    for task_id in sorted(set(old_by_task) | set(new_by_task)):
        old = sorted(old_by_task.get(task_id, []), key=lambda row: row["start_at"])
        new = sorted(new_by_task.get(task_id, []), key=lambda row: row["start_at"])
        title = (new or old)[0].get("title") or "Study block"

        if old and new:
            old_ranges = [(row["start_at"], row["end_at"]) for row in old]
            new_ranges = [(row["start_at"], row["end_at"]) for row in new]
            bucket = "unchanged" if old_ranges == new_ranges else "moved"
            result[bucket].append(
                {
                    "task_id": task_id,
                    "title": title,
                    "from_start": old_ranges[0][0],
                    "from_end": old_ranges[0][1],
                    "to_start": new_ranges[0][0],
                    "to_end": new_ranges[0][1],
                    "reason": (
                        "unchanged"
                        if bucket == "unchanged"
                        else "moved to fit the revised plan"
                    ),
                }
            )
        elif new:
            result["added"].append(
                {
                    "task_id": task_id,
                    "title": title,
                    "from_start": None,
                    "from_end": None,
                    "to_start": new[0]["start_at"],
                    "to_end": new[0]["end_at"],
                    "reason": "new in this proposal",
                }
            )
        else:
            result["removed"].append(
                {
                    "task_id": task_id,
                    "title": title,
                    "from_start": old[0]["start_at"],
                    "from_end": old[0]["end_at"],
                    "to_start": None,
                    "to_end": None,
                    "reason": "no longer scheduled",
                }
            )

    return result


def build_proposal(
    pending: Any,
    plan_data: Mapping[str, Any],
    *,
    conflicts: list[dict] | None = None,
) -> dict:
    """Build the modal's proposed_plan object."""
    plan = plan_data.get("plan") or {}
    items = plan_items_from_rows(plan_data.get("items") or [])
    diff = normalize_diff(getattr(pending, "diff", None) or {}, items)
    notes = [item.get("note", "") for item in (conflicts or []) if item.get("note")]

    return {
        "plan_id": getattr(pending, "plan_id", None) or plan.get("plan_id"),
        "summary": getattr(pending, "summary", None)
        or plan.get("summary")
        or "Proposed plan",
        "diff": diff,
        "items": items,
        "warnings": notes,
        "conflicts": notes,
    }


def friendly_notice(
    *, used_fallback: bool, error: str | None, has_key: bool
) -> str | None:
    if not used_fallback:
        return None
    # FIX (deploy-ready, item 3): a spent live-AI budget is expected, not an
    # error. Say so plainly and keep the turn in quick mode. The controller
    # passes the exception text, so match both the class name and its wording.
    if "LiveLimitReached" in (error or "") or "limit reached" in (error or "").lower():
        return "Live AI limit reached; using fallback mode."
    lowered = (error or "").lower()
    if not has_key:
        return "Live AI is off (no GEMINI_API_KEY); quick mode is handling this turn."
    if "429" in lowered or "quota" in lowered or "resource_exhausted" in lowered:
        return "The live AI is busy (free-tier quota); quick mode handled this turn."
    return "Live AI was unavailable; quick mode handled this turn."


def map_chat_response(
    response: Any,
    *,
    pending_data: Mapping[str, Any] | None,
    conflicts: list[dict] | None,
    has_key: bool,
) -> dict:
    """Map AgentResponse -> the exact /chat JSON shape."""
    proposal = None
    if getattr(response, "pending_confirmation", None) is not None:
        proposal = build_proposal(
            response.pending_confirmation,
            pending_data or {"plan": None, "items": []},
            conflicts=conflicts,
        )

    return {
        "reply": response.reply_text,
        "tool_calls": map_tool_calls(
            response.tool_calls, conflicts=len(conflicts or [])
        ),
        "proposed_plan": proposal,
        "used_fallback": bool(response.used_fallback),
        "notice": friendly_notice(
            used_fallback=bool(response.used_fallback),
            error=getattr(response, "error", None),
            has_key=has_key,
        ),
    }
