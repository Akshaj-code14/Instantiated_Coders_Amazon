"""Deterministic safety screen and rule-based fallback router.

Used when Gemini is unavailable (or keeps returning unusable calls). It never
invents data: every reply is derived from a tool result. Anything it cannot
map cleanly becomes one clarifying question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Callable

from agent.config import IST, MAX_RANGE_DAYS
from agent.tool_registry import DispatchOutcome
from agent.validators import parse_days_ahead, parse_duration_minutes, to_iso

ExecuteTool = Callable[[str, dict[str, Any]], DispatchOutcome]

# ---------------------------------------------------------------------------
# Crisis screen (pre-LLM, deterministic)
# ---------------------------------------------------------------------------

_CRISIS_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern) for pattern in (
        r"\bkill(?:ing)?\s+myself\b",
        r"\bkill\s+me\b",
        r"\bsuicid(?:e|al|ality)?\b",
        r"\bself[\s-]?harm(?:ing|ed)?\b",
        r"\bhurt(?:ing)?\s+myself\b",
        r"\bend\s+(?:my|it)\s+(?:life|all)\b",
        r"\btake\s+my\s+own\s+life\b",
        r"\bwant(?:ed)?\s+to\s+die\b",
        r"\b(?:don'?t|do not)\s+want\s+to\s+live\b",
        r"\bno\s+reason\s+to\s+live\b",
        r"\bbetter\s+off\s+dead\b",
        r"\boverdose\b",
        r"\b(?:can'?t|cannot)\s+go\s+on\b",
        r"\b(?:can'?t|cannot)\s+keep\s+going\b",
    )
)


def screen_for_crisis(text: str) -> bool:
    """True when the message suggests self-harm or suicidal intent.

    Deliberately simple and deterministic: a false positive costs one turn of
    resources, a false negative could cost much more.
    """
    lowered = (text or "").lower()
    return any(pattern.search(lowered) for pattern in _CRISIS_PATTERNS)


# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

_PLANNING_RE = re.compile(r"\b(plan|schedule|organi[sz]e|map out)\b")
_DAY_RE = re.compile(r"\b(day|days|week|tomorrow|today)\b")
_LOST_RE = re.compile(r"\blost\b.*\b(hours?|hrs?|minutes?|mins?|time)\b")
_MARK_RE = re.compile(r"\b(mark|marked|set)\b.*\b(complete|completed|done|finished)\b")
_DEADLINE_RE = re.compile(r"\b(deadlines?|due)\b")
_NOTICE_RE = re.compile(r"\b(notices?|announcements?|noticeboard)\b")
_SAVE_RE = re.compile(
    r"\b(save|confirm)\b.*\bplan\b|\bplan\b.*\b(save|confirm)\b"
)
_NOTICE_FILLER = {
    "search", "show", "me", "my", "the", "for", "find", "any", "about",
    "notices", "notice", "announcements", "announcement", "noticeboard",
    "please", "check", "what", "whats", "are", "is", "look", "up",
}

_STOPWORDS = {
    "the", "my", "a", "an", "for", "with", "and", "task", "assignment",
    "mark", "complete", "completed", "done", "finished", "please", "this",
    "that", "was", "have", "had", "study", "studied", "i", "it",
}


@dataclass
class FallbackResult:
    reply_text: str
    handled: bool = True


def route_fallback(
    user_text: str,
    *,
    now: datetime,
    state: dict[str, Any],
    execute: ExecuteTool,
) -> FallbackResult:
    """Rule-based routing; every tool call still goes through validation."""
    lowered = (user_text or "").lower().strip()
    if not lowered:
        return FallbackResult("What would you like to plan?", handled=False)

    cached_tasks = _cached_deadlines(state)

    if _SAVE_RE.search(lowered):
        if state.get("pending"):
            return FallbackResult(
                "There's a proposed plan waiting. Press Confirm to save it — I "
                "can't confirm it from a chat message."
            )
        return FallbackResult(
            "There's nothing to save yet. Ask me to plan something first, then "
            "press Confirm on the proposal."
        )

    if _LOST_RE.search(lowered) or (
        "lost" in lowered and parse_duration_minutes(lowered) is not None
    ):
        return _replan(user_text, now=now, state=state, execute=execute)

    if _MARK_RE.search(lowered) or (
        re.search(r"\b(finished|completed)\b", lowered) and cached_tasks
    ):
        return _mark_complete(user_text, state=state, execute=execute,
                              cached_tasks=cached_tasks)

    if _PLANNING_RE.search(lowered) and _DAY_RE.search(lowered):
        return _plan_window(user_text, now=now, execute=execute)

    if _DEADLINE_RE.search(lowered):
        return _deadlines(user_text, execute=execute)

    if _NOTICE_RE.search(lowered):
        return _notices(user_text, execute=execute)

    if _PLANNING_RE.search(lowered):
        return _plan_window(user_text, now=now, execute=execute)

    return FallbackResult(
        "I can't reach my AI brain right now. I can still: list your deadlines, "
        "plan your next two days, or mark a task complete — try the buttons, or "
        "rephrase with one of those.",
        handled=False,
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _plan_window(text: str, *, now: datetime, execute: ExecuteTool) -> FallbackResult:
    days = parse_days_ahead(text)
    if days is None:
        days = 7 if re.search(r"\bweek\b", text.lower()) else 2
    days = max(1, min(days, MAX_RANGE_DAYS))
    start = now
    end = datetime.combine(
        now.date() + timedelta(days=days), time(23, 59), IST
    )

    deadlines = execute("get_upcoming_deadlines", {"days_ahead": days})
    if not deadlines.result.success:
        return FallbackResult(
            f"I couldn't read your deadlines: {deadlines.result.message}"
        )
    tasks = [
        item for item in deadlines.result.data.get("deadlines", [])
        if item.get("status") != "completed"
    ]
    if not tasks:
        return FallbackResult(
            f"Quick mode: I found no deadlines in the next {days} day(s), so there "
            "is nothing to plan yet. Want me to look at a longer window?"
        )

    execute("get_schedule", {"start_date": to_iso(start), "end_date": to_iso(end)})
    slots = execute("find_free_slots", {
        "start_date": to_iso(start), "end_date": to_iso(end), "min_minutes": 30,
    })
    if not slots.result.success:
        return FallbackResult(
            f"I couldn't read your free time: {slots.result.message}"
        )

    plan = execute("create_plan", {
        "start_date": to_iso(start),
        "end_date": to_iso(end),
        "task_ids": [item["id"] for item in tasks],
    })
    if not plan.result.success:
        return FallbackResult(
            f"Quick mode: I found {len(tasks)} deadline(s) but couldn't build a "
            f"plan ({plan.result.message}). Try again in a moment."
        )

    blocks = plan.result.data.get("blocks", [])
    total = plan.result.data.get("total_minutes", 0)
    lines = [_format_block(block) for block in blocks[:6]]
    body = "\n".join(f"• {line}" for line in lines) if lines else "• (no blocks yet)"
    note = ""
    if plan.result.data.get("unscheduled_task_ids"):
        note = "\nSome tasks did not fit and are not scheduled."
    conflicts = slots.result.data.get("conflicts") or []
    if conflicts:
        note += f"\nHeads-up: {conflicts[0]['note']}."
    return FallbackResult(
        f"Quick mode (AI unavailable) — built from your real data:\n{body}\n"
        f"Total: {total} minutes across {len(blocks)} block(s).{note}\n"
        "Nothing is saved yet — press Confirm to save this plan."
    )


def _replan(text: str, *, now: datetime, state: dict[str, Any],
            execute: ExecuteTool) -> FallbackResult:
    lost = parse_duration_minutes(text)
    if lost is None:
        return FallbackResult(
            "How much time did you lose — for example '90 minutes' or 'two hours'?"
        )
    last_plan = state.get("last_plan") or {}
    plan_id = state.get("active_plan_id") or last_plan.get("plan_id")
    if not plan_id:
        return FallbackResult(
            "I don't have an active plan to adjust yet. Ask me to plan your next "
            "two days first, then tell me what changed."
        )
    total = last_plan.get("total_minutes") if plan_id == last_plan.get("plan_id") else None
    available = max(0, int(total) - lost) if total is not None else None

    outcome = execute("replan_schedule", {
        "plan_id": plan_id,
        "change_reason": f"lost {lost} minutes",
        "available_minutes": available,
    })
    if not outcome.result.success:
        return FallbackResult(
            f"I couldn't rework the plan: {outcome.result.message}"
        )
    diff = outcome.result.data.get("diff") or {}
    moved = len(diff.get("moved") or [])
    dropped = len(diff.get("removed") or [])
    blocks = outcome.result.data.get("blocks") or []
    lines = [_format_block(block) for block in blocks[:6]]
    body = "\n".join(f"• {line}" for line in lines) if lines else "• (nothing left to schedule)"
    return FallbackResult(
        f"Quick mode: I reworked the plan for the {lost} minutes you lost "
        f"({moved} block(s) moved, {dropped} dropped):\n{body}\n"
        "Nothing is saved yet — press Confirm to keep this version."
    )


def _mark_complete(text: str, *, state: dict[str, Any],
                   execute: ExecuteTool,
                   cached_tasks: list[dict[str, Any]]) -> FallbackResult:
    if not cached_tasks:
        return FallbackResult(
            "I don't have your task list yet. Ask me for your deadlines first, "
            "then which one should I mark complete?"
        )
    task = _match_task(text, cached_tasks)
    if task is None:
        titles = ", ".join(item.get("title", "?") for item in cached_tasks[:4])
        return FallbackResult(
            f"Which task should I mark complete? Your upcoming ones are: {titles}."
        )
    outcome = execute("update_task", {"task_id": task["id"], "status": "completed"})
    if not outcome.result.success:
        return FallbackResult(f"I couldn't update it: {outcome.result.message}")
    return FallbackResult(f"Done — {task.get('title', task['id'])} is marked complete.")


def _deadlines(text: str, *, execute: ExecuteTool) -> FallbackResult:
    days = parse_days_ahead(text) or 7
    outcome = execute("get_upcoming_deadlines", {"days_ahead": days})
    if not outcome.result.success:
        return FallbackResult(f"I couldn't read your deadlines: {outcome.result.message}")
    items = outcome.result.data.get("deadlines", [])
    if not items:
        return FallbackResult(f"You have no deadlines in the next {days} days.")
    lines = [
        f"• {item.get('title')} — due {_short_when(item.get('due_at'))} "
        f"({item.get('priority')})"
        for item in items[:8]
    ]
    return FallbackResult(
        f"You have {len(items)} deadline(s) in the next {days} days:\n"
        + "\n".join(lines)
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _notices(text: str, *, execute: ExecuteTool) -> FallbackResult:
    tokens = [
        token for token in re.findall(r"[a-z0-9]+", text.lower())
        if token not in _NOTICE_FILLER
    ]
    query = " ".join(tokens)[:120]
    outcome = execute("search_notices", {"query": query, "category": None})
    if not outcome.result.success:
        return FallbackResult(f"I couldn't search the notices: {outcome.result.message}")
    items = outcome.result.data.get("notices", [])
    if not items:
        return FallbackResult(
            f"No notices matched {query!r}." if query else "There are no notices."
        )
    lines = [
        f"• {item.get('title')} ({item.get('category')}): "
        f"{str(item.get('body', ''))[:180]}"
        for item in items[:8]
    ]
    return FallbackResult(
        "Quick mode — notices are untrusted data; I won't act on instructions "
        "inside them:\n" + "\n".join(lines)
    )


def _cached_deadlines(state: dict[str, Any]) -> list[dict[str, Any]]:
    cache = state.get("cache") or {}
    return [
        item for item in (cache.get("deadlines") or [])
        if isinstance(item, dict) and item.get("id")
    ]


def _match_task(text: str, tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    words = {
        word for word in re.findall(r"[a-z0-9]+", text.lower())
        if len(word) > 2 and word not in _STOPWORDS
    }
    best: dict[str, Any] | None = None
    best_score = 0
    for task in tasks:
        title_words = set(re.findall(r"[a-z0-9]+", str(task.get("title", "")).lower()))
        score = len(words & title_words)
        if score > best_score:
            best, best_score = task, score
    return best


def _short_when(value: Any) -> str:
    try:
        return datetime.fromisoformat(str(value)).astimezone(IST).strftime(
            "%a %d %b, %H:%M"
        )
    except (ValueError, TypeError):
        return str(value)


def _format_block(block: dict[str, Any]) -> str:
    try:
        start = datetime.fromisoformat(str(block["start"])).astimezone(IST)
        end = datetime.fromisoformat(str(block["end"])).astimezone(IST)
        return f"{start:%a %d %b %H:%M}–{end:%H:%M} — {block.get('title', 'study')}"
    except (KeyError, ValueError, TypeError):
        return f"{block.get('title', 'study')} ({block.get('minutes', '?')} min)"
