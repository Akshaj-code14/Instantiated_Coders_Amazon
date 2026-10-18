"""The agent loop.

Flow for one message:
    crisis screen -> history -> LLM turn(s) -> validated tool calls -> reply

Invariants enforced in code (never trusted to the model):
  * ``student_id`` is injected from the session.
  * IDs must have appeared in an earlier tool result this session.
  * ``save_plan`` is never executed from ``handle_message``; it becomes a
    ``PendingConfirmation`` that only ``confirm_pending`` can act on.
  * Every failure becomes a message, never an exception.
  * State is a plain dict and always JSON-serializable.

No Streamlit import. No module-level mutable state.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from pydantic import ValidationError

from agent.config import IST, AgentConfig, load_config
from agent.fallback_router import route_fallback, screen_for_crisis
from agent.llm_client import (
    GeminiLLMClient,
    LLMClient,
    LLMUnavailableError,
    LLMTurn,
)
from agent.prompts import build_system_prompt
from agent.schemas import (
    MUTATING_TOOLS,
    AgentResponse,
    PendingConfirmation,
    ToolCallTrace,
    ToolResult,
)
from agent.tool_registry import DispatchOutcome, build_tools, dispatch
from agent.validators import (
    SeenIds,
    ToolValidationError,
    collect_ids,
    parse_aware_datetime,
    parse_duration_minutes,
    to_iso,
    validate_student_id,
)

logger = logging.getLogger(__name__)

_STATE_VERSION = 1
_MAX_MESSAGE_CHARS = 2000
_MAX_HISTORY_PAYLOAD_CHARS = 8000
_SAVED_CLAIM_RE = re.compile(
    r"\b(?:i(?:'ve| have)?\s+saved|saved\s+(?:it|your|the|plan|everything)|"
    r"it'?s\s+saved|(?:plan|everything)\s+is\s+(?:now\s+)?saved|"
    r"has\s+been\s+saved|have\s+been\s+saved)\b",
    re.IGNORECASE,
)
_SAVED_NEGATIONS = (
    "not saved", "nothing is saved", "nothing was saved", "can't save",
    "cannot save", "couldn't save", "won't save", "before it is saved",
)
_LOST_RE = re.compile(r"\b(lost|lose|losing|less time|short on time)\b", re.IGNORECASE)
_AVAILABLE_RE = re.compile(
    r"\b(only have|i have|left with|available|free)\b", re.IGNORECASE
)
_ENERGY_WORDS = (
    "energy", "tired", "exhausted", "drained", "sleepy", "wired", "fresh",
)


class _ModelOutputError(RuntimeError):
    """Two consecutive unusable model turns: hand over to the fallback router."""


def _empty_state() -> dict[str, Any]:
    return {
        "version": _STATE_VERSION,
        "history": [],
        "seen_ids": [],
        "pending": None,
        "active_plan_id": None,
        "last_plan": None,
        "cache": {},
        "context": {},
        "suggested_task": None,
        "last_action": None,
    }


class CampusPilotAgent:
    """Conversational agent for one student session."""

    def __init__(
        self,
        student_id: str,
        backend: Any,
        llm_client: LLMClient | None = None,
        state: dict[str, Any] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        config: AgentConfig | None = None,
    ) -> None:
        validate_student_id(student_id)
        self.student_id = student_id
        self._backend = backend
        self._config = config or load_config()
        self._llm: LLMClient = llm_client or GeminiLLMClient(self._config)
        self._clock = clock or (lambda: datetime.now(IST))
        self._state = self._normalise_state(state)
        self._seen = SeenIds(self._state["seen_ids"])
        self._turn_seen_calls: set[str] = set()
        self._turn_text: str = ""

    # ------------------------------------------------------------------
    # Public API (Person 3 calls exactly these)
    # ------------------------------------------------------------------

    def handle_message(self, user_text: str) -> AgentResponse:
        text = (user_text or "").strip()[:_MAX_MESSAGE_CHARS]
        trace: list[ToolCallTrace] = []
        self._turn_seen_calls = set()
        self._turn_text = text

        if not text:
            return AgentResponse(reply_text="What would you like to plan?")

        if screen_for_crisis(text):
            return self._wellbeing_turn(text, trace)

        self._append_history({"role": "user", "text": text})

        try:
            reply = self._run_loop(trace)
            reply = self._guard_saved_claim(reply, trace)
            reply = self._add_followup_suggestion(reply, trace)
            self._finish_reply(reply)
            return AgentResponse(
                reply_text=reply,
                tool_calls=trace,
                pending_confirmation=self._pending_model(),
                used_fallback=False,
            )
        except _ModelOutputError as exc:
            logger.warning("falling back: %s", exc)
            return self._fallback_turn(text, trace, reason=str(exc))
        except LLMUnavailableError as exc:
            logger.warning("LLM unavailable: %s", exc)
            return self._fallback_turn(text, trace, reason=str(exc))
        except Exception as exc:  # noqa: BLE001 - the UI must never see a traceback
            logger.exception("unhandled controller failure")
            reply = (
                "Something went wrong on my side, and nothing was saved. "
                "Could you try that again in a moment?"
            )
            self._finish_reply(reply)
            return AgentResponse(
                reply_text=reply,
                tool_calls=trace,
                pending_confirmation=self._pending_model(),
                error=f"{type(exc).__name__}: {exc}"[:300],
                used_fallback=False,
            )

    def confirm_pending(self) -> AgentResponse:
        """Called by the UI's "Confirm and save" button. The only write path."""
        trace: list[ToolCallTrace] = []
        pending = self._state.get("pending")
        if not pending:
            return AgentResponse(
                reply_text="There's no plan waiting for confirmation.",
                error="no_pending",
            )

        plan_id = str(pending.get("plan_id", ""))
        outcome = dispatch(
            "save_plan",
            {"plan_id": plan_id},
            student_id=self.student_id,
            backend=self._backend,
            seen_ids=self._seen,
            allow_gated=True,
        )
        self._record_trace(
            trace, "save_plan", outcome.args, outcome.result,
            reason="confirmed by the student via the UI",
        )
        self._state["seen_ids"] = self._seen.to_list()

        if outcome.result.success:
            self._state["pending"] = None
            self._state["active_plan_id"] = plan_id
            self._state["last_action"] = f"saved plan {plan_id}"
            reply = f"Saved — plan {plan_id} is on your schedule now."
            note = f"[UI] The student confirmed; plan {plan_id} was saved."
        else:
            reply = f"I couldn't save the plan: {outcome.result.message}"
            note = f"[UI] The student confirmed, but saving plan {plan_id} failed."

        self._append_history({"role": "user", "text": note})
        self._finish_reply(reply)
        return AgentResponse(
            reply_text=reply,
            tool_calls=trace,
            pending_confirmation=self._pending_model(),
            error=None if outcome.result.success else outcome.result.message,
        )

    def cancel_pending(self) -> AgentResponse:
        trace: list[ToolCallTrace] = []
        pending = self._state.get("pending")
        if not pending:
            return AgentResponse(
                reply_text="There's no plan waiting for confirmation.",
                error="no_pending",
            )
        plan_id = pending.get("plan_id")
        self._state["pending"] = None
        self._state["last_action"] = f"cancelled plan {plan_id}"
        reply = "Okay — I won't save that plan. Nothing was changed."
        self._append_history(
            {"role": "user", "text": f"[UI] The student cancelled plan {plan_id}."}
        )
        self._finish_reply(reply)
        return AgentResponse(reply_text=reply, tool_calls=trace)

    def export_state(self) -> dict[str, Any]:
        """JSON-serializable session state for ``st.session_state``."""
        self._state["seen_ids"] = self._seen.to_list()
        self._state["version"] = _STATE_VERSION
        # No default=str: leaking a datetime here is a bug we want to see.
        return json.loads(json.dumps(self._state))

    # ------------------------------------------------------------------
    # LLM loop
    # ------------------------------------------------------------------

    def _run_loop(self, trace: list[ToolCallTrace]) -> str:
        corrections = 0
        invalid_streak = 0

        for _ in range(max(1, self._config.max_tool_iterations)):
            turn = self._llm.generate(
                system_prompt=self._system_prompt(),
                history=list(self._state["history"]),
                tools=build_tools(),
            )
            self._append_history(self._turn_to_history(turn))

            if not turn.function_calls:
                if turn.text.strip():
                    return turn.text.strip()
                # Do not leave an empty model turn in history.
                self._state["history"].pop()
                raise _ModelOutputError("model returned an empty turn")

            results: list[dict[str, Any]] = []
            turn_had_success = False
            turn_was_all_invalid = True

            for call in turn.function_calls:
                key = _call_key(call.name, call.args)
                if key in self._turn_seen_calls:
                    corrections += 1
                    result = ToolResult(
                        success=False,
                        tool_name=call.name or "unknown_tool",
                        data={},
                        message=(
                            "You already called this tool with these exact "
                            "arguments in this turn. Use the earlier result "
                            "instead of repeating the call."
                        ),
                    )
                    self._record_trace(trace, call.name, call.args, result,
                                       "duplicate call blocked")
                    results.append(self._history_result(call.name, result))
                    continue
                self._turn_seen_calls.add(key)

                if call.name == "save_plan":
                    # Confirmation gate: never executes here.
                    result = self._await_confirmation(call.args)
                    self._record_trace(
                        trace, "save_plan", call.args, result,
                        "confirmation gate: waiting for the student",
                    )
                    results.append(self._history_result("save_plan", result))
                    turn_was_all_invalid = False
                    continue

                outcome = self._execute(call.name, call.args, trace)
                results.append(self._history_result(call.name, outcome.result))
                if outcome.result.success:
                    turn_had_success = True
                if outcome.validation_error:
                    corrections += 1
                else:
                    turn_was_all_invalid = False

            self._append_history({"role": "tool", "results": results})
            self._trim_history()

            if corrections > self._config.max_corrections:
                return (
                    "I couldn't turn that into a valid action. Could you tell me "
                    "once more what you want, with the date or task name?"
                )
            if turn_was_all_invalid and not turn_had_success:
                invalid_streak += 1
                if invalid_streak >= 2:
                    raise _ModelOutputError("two consecutive invalid model turns")
            else:
                invalid_streak = 0

        return self._iteration_cap_reply(trace)

    def _iteration_cap_reply(self, trace: list[ToolCallTrace]) -> str:
        pending = self._state.get("pending")
        if pending:
            return (
                "I've put together a plan proposal. Nothing is saved yet — "
                "review it and press Confirm to save it."
            )
        successes = [item for item in trace if item.success]
        if successes:
            summary = "; ".join(item.short_result for item in successes[-3:])
            return (
                "I reached my step limit for this turn. What I gathered: "
                f"{summary}. Could you narrow it down so I can finish?"
            )
        return (
            "I reached my step limit without completing that. Could you rephrase "
            "it more specifically — for example 'plan my next two days'?"
        )

    # ------------------------------------------------------------------
    # Fallback + wellbeing
    # ------------------------------------------------------------------

    def _fallback_turn(
        self, text: str, trace: list[ToolCallTrace], *, reason: str
    ) -> AgentResponse:
        mutated = any(
            item.success and item.name in MUTATING_TOOLS for item in trace
        )
        if mutated:
            reply = (
                "I lost my AI connection partway through, but these actions did "
                "go through: "
                + "; ".join(item.short_result for item in trace if item.success)
                + ". Nothing else was changed."
            )
        else:
            result = route_fallback(
                text,
                now=self._now(),
                state=self._state,
                execute=lambda name, args: self._execute(name, args, trace),
            )
            reply = result.reply_text

        reply = self._guard_saved_claim(reply, trace)
        self._finish_reply(reply)
        return AgentResponse(
            reply_text=reply,
            tool_calls=trace,
            pending_confirmation=self._pending_model(),
            error=reason[:300],
            used_fallback=True,
        )

    def _wellbeing_turn(self, text: str,
                        trace: list[ToolCallTrace]) -> AgentResponse:
        # Record the message for continuity, then stop scheduling entirely.
        self._append_history({"role": "user", "text": text})
        outcome = self._execute(
            "get_wellbeing_support",
            {"risk_flag": True},
            trace,
            reason="pre-LLM crisis screen",
        )
        if outcome.result.success:
            resources = outcome.result.data.get("resources", [])
            lines = [
                f"• {item.get('name')} — {item.get('contact')} ({item.get('hours')})"
                for item in resources
                if item.get("name")
            ]
            reply = (
                outcome.result.message
                + ("\n\n" + "\n".join(lines) if lines else "")
                + "\n\nIf you're in immediate danger, call 112 (India) or your "
                "local emergency number. I won't schedule anything right now."
            )
        else:
            reply = (
                "I'm really sorry you're going through this. Please reach out to "
                "someone you trust or your campus counselling centre right now. "
                "If you're in immediate danger, call 112 (India). I've paused "
                "planning for now."
            )
        self._finish_reply(reply)
        return AgentResponse(
            reply_text=reply,
            tool_calls=trace,
            pending_confirmation=None,
            used_fallback=False,
        )

    # ------------------------------------------------------------------
    # Tool execution + state bookkeeping
    # ------------------------------------------------------------------

    def _execute(
        self,
        name: str,
        args: Any,
        trace: list[ToolCallTrace],
        *,
        reason: str | None = None,
    ) -> DispatchOutcome:
        prepared = self._prepare_args(name, args)
        if isinstance(prepared, ToolResult):
            self._record_trace(trace, name, args if isinstance(args, dict) else {},
                               prepared, reason or "needs clarification")
            return DispatchOutcome(
                result=prepared, args=args if isinstance(args, dict) else {}
            )
        args = prepared
        outcome = dispatch(
            name,
            args,
            student_id=self.student_id,
            backend=self._backend,
            seen_ids=self._seen,
            allow_gated=False,
        )
        self._record_trace(
            trace, name, outcome.args, outcome.result, reason
        )
        self._after_tool(outcome)
        self._state["seen_ids"] = self._seen.to_list()
        return outcome

    def _after_tool(self, outcome: DispatchOutcome) -> None:
        result = outcome.result
        if not result.success:
            return
        data = result.data
        tool = result.tool_name
        cache = self._state.setdefault("cache", {})

        if tool == "get_upcoming_deadlines":
            cache["deadlines"] = data.get("deadlines", [])
        elif tool == "get_schedule":
            cache["events"] = data.get("events", [])
        elif tool == "find_free_slots":
            cache["free_slots"] = data.get("free_slots", [])
        elif tool == "get_student_context":
            student = data.get("student") or {}
            self._state["context"] = {
                "student_name": student.get("name"),
                "prefs": student.get("preferences") or {},
            }
            if data.get("active_plan_id"):
                self._state["active_plan_id"] = data["active_plan_id"]
        elif tool in ("create_plan", "replan_schedule"):
            plan_id = data.get("plan_id")
            if plan_id:
                blocks = data.get("blocks") or []
                self._state["last_plan"] = {
                    "plan_id": plan_id,
                    "total_minutes": data.get("total_minutes"),
                    "blocks": blocks[:50],
                    "task_ids": outcome.args.get("task_ids") or [],
                    "created_at": to_iso(self._now()),
                }
                if result.requires_confirmation:
                    self._store_pending(result, kind=tool)
        elif tool == "create_task":
            task_id = data.get("task_id")
            if task_id:
                cache.setdefault("deadlines", []).append({
                    "id": task_id,
                    "title": data.get("title"),
                    "due_at": data.get("due_at"),
                    "priority": data.get("priority"),
                    "status": data.get("status", "pending"),
                })
        elif tool == "save_plan":
            if data.get("status") == "saved":
                self._state["active_plan_id"] = data.get("plan_id")
                self._state["pending"] = None
        elif tool == "update_task":
            task_id = data.get("task_id")
            for item in cache.get("deadlines", []) or []:
                if item.get("id") == task_id:
                    item["status"] = data.get("status", item.get("status"))
                    if data.get("due_at"):
                        item["due_at"] = data["due_at"]
        elif tool == "log_checkin":
            blocker = (data.get("blocker") or "").strip()
            if blocker:
                self._state["suggested_task"] = {
                    "title": f"Follow up: {blocker}"[:200],
                    "event_id": data.get("event_id"),
                    "source": "log_checkin",
                }

    def _store_pending(self, result: ToolResult, *, kind: str) -> None:
        data = result.data
        plan_id = data.get("plan_id")
        if not plan_id:
            return
        blocks = data.get("blocks") or []
        total = data.get("total_minutes")
        if total is None:
            total = sum(int(block.get("minutes") or 0) for block in blocks)
        diff = data.get("diff") or {}
        if kind == "create_plan":
            summary = f"{len(blocks)} block(s), {total} minutes of study planned"
        else:
            moved = len(diff.get("moved") or [])
            dropped = len(diff.get("removed") or [])
            summary = (
                f"reworked plan: {len(blocks)} block(s), {total} minutes "
                f"({moved} moved, {dropped} dropped)"
            )
        self._state["pending"] = {
            "plan_id": str(plan_id),
            "summary": summary,
            "diff": diff,
            "kind": kind,
            "created_at": to_iso(self._now()),
        }

    def _prepare_args(self, name: str, args: Any) -> Any | ToolResult:
        """Deterministic fix-ups the model must not do for itself.

        * replan_schedule's ``available_minutes`` comes from the student's own
          words and our stored plan total, never from model arithmetic.
        * log_checkin's mood/energy are kept only if the student stated them.
        """
        if not isinstance(args, dict):
            return args
        prepared = dict(args)
        if name in ("get_schedule", "find_free_slots", "create_plan"):
            raw_end = prepared.get("end_date")
            if isinstance(raw_end, str):
                try:
                    end = parse_aware_datetime(raw_end, "end_date")
                except ToolValidationError:
                    end = None  # let schema validation produce the date message
                if end is not None and end < self._now() - timedelta(days=1):
                    return ToolResult(
                        success=False,
                        tool_name=name,
                        data={},
                        message=(
                            "That date range is in the past, so there is nothing "
                            "to schedule. What upcoming dates should I use?"
                        ),
                    )
        if name == "replan_schedule":
            duration = parse_duration_minutes(self._turn_text)
            if duration is None:
                return ToolResult(
                    success=False,
                    tool_name="replan_schedule",
                    data={},
                    message=(
                        "How much time did you lose — for example '90 minutes' or "
                        "'two hours'? I need that before I can rework the plan."
                    ),
                )
            last_plan = self._state.get("last_plan") or {}
            total = None
            if last_plan.get("plan_id") == prepared.get("plan_id"):
                total = last_plan.get("total_minutes")
            if _LOST_RE.search(self._turn_text) or "lost" in self._turn_text.lower():
                prepared["available_minutes"] = (
                    max(0, int(total) - duration) if total is not None else None
                )
            elif _AVAILABLE_RE.search(self._turn_text):
                prepared["available_minutes"] = duration
            else:
                prepared["available_minutes"] = (
                    max(0, int(total) - duration) if total is not None else None
                )
        elif name == "log_checkin":
            mood = prepared.get("mood")
            if mood and not self._stated_in_text(str(mood)):
                prepared["mood"] = None
            if prepared.get("energy") is not None and not any(
                word in self._turn_text.lower() for word in _ENERGY_WORDS
            ):
                prepared["energy"] = None
        return prepared

    def _stated_in_text(self, value: str) -> bool:
        lowered = self._turn_text.lower()
        tokens = [token for token in re.findall(r"[a-z]{3,}", value.lower())]
        return any(token in lowered for token in tokens)

    def _add_followup_suggestion(
        self, reply: str, trace: list[ToolCallTrace]
    ) -> str:
        logged = [item for item in trace if item.name == "log_checkin" and item.success]
        created = any(
            item.name == "create_task" and item.success for item in trace
        )
        suggestion = self._state.get("suggested_task")
        if not logged or created or not suggestion:
            return reply
        title = suggestion.get("title", "")[:120]
        if not title or title.lower() in reply.lower():
            return reply
        return reply.rstrip() + f'\n\nWant me to add a follow-up task: “{title}”?'

    def _await_confirmation(self, raw_args: Any) -> ToolResult:
        args = raw_args if isinstance(raw_args, dict) else {}
        last_plan = self._state.get("last_plan") or {}
        plan_id = args.get("plan_id") or last_plan.get("plan_id")

        if not plan_id or plan_id != last_plan.get("plan_id"):
            return ToolResult(
                success=False,
                tool_name="save_plan",
                data={},
                message=(
                    "I don't have that proposed plan in this session. Build a plan "
                    "first, then ask the student to confirm it."
                ),
            )

        if not self._state.get("pending"):
            self._state["pending"] = {
                "plan_id": str(plan_id),
                "summary": "plan proposal",
                "diff": {},
                "kind": "create_plan",
                "created_at": to_iso(self._now()),
            }
        return ToolResult(
            success=False,
            tool_name="save_plan",
            data={"plan_id": plan_id},
            message=(
                "Plan not saved yet: awaiting user confirmation. The student must "
                "press Confirm. Do not say it is saved."
            ),
            requires_confirmation=True,
            next_suggested_tool="save_plan",
        )

    def _pending_model(self) -> PendingConfirmation | None:
        pending = self._state.get("pending")
        if not pending:
            return None
        try:
            return PendingConfirmation(
                plan_id=str(pending.get("plan_id", "")),
                summary=str(pending.get("summary", "")),
                diff=pending.get("diff") or {},
            )
        except ValidationError:
            logger.error("stored pending confirmation is invalid")
            return None

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    @property
    def _history(self) -> list[dict[str, Any]]:
        return self._state["history"]

    def _append_history(self, message: dict[str, Any]) -> None:
        self._state["history"].append(message)

    def _finish_reply(self, reply: str) -> None:
        """Append a model turn unless the loop already recorded this exact one."""
        history = self._state["history"]
        last = history[-1] if history else None
        already_there = (
            isinstance(last, dict)
            and last.get("role") == "model"
            and last.get("text") == reply
            and not last.get("function_calls")
        )
        if not already_there:
            history.append({"role": "model", "text": reply})
        self._trim_history()

    def _trim_history(self) -> None:
        limit = max(4, self._config.max_history_turns * 2)
        history = self._state["history"]
        if len(history) <= limit:
            return
        cut = len(history) - limit
        while cut < len(history) and history[cut].get("role") != "user":
            cut += 1
        if cut >= len(history):
            return
        self._state["history"] = history[cut:]

    @staticmethod
    def _turn_to_history(turn: LLMTurn) -> dict[str, Any]:
        return {
            "role": "model",
            "text": turn.text,
            "function_calls": [
                {
                    "name": call.name,
                    "args": call.args,
                    "thought_signature": call.thought_signature,
                }
                for call in turn.function_calls
            ],
        }

    def _history_result(self, name: str,
                        result: ToolResult) -> dict[str, Any]:
        payload = {
            "success": result.success,
            "tool_name": result.tool_name,
            "data": _trim_payload(result.data),
            "message": result.message[:400],
            "requires_confirmation": result.requires_confirmation,
            "next_suggested_tool": result.next_suggested_tool,
        }
        if len(json.dumps(payload)) > _MAX_HISTORY_PAYLOAD_CHARS:
            payload["data"] = {
                "note": "result truncated for length; IDs preserved",
                "ids": sorted(collect_ids(result.data)),
            }
        return {"name": name, "response": payload}

    @staticmethod
    def _record_trace(
        trace: list[ToolCallTrace],
        name: str,
        args: Any,
        result: ToolResult,
        reason: str | None = None,
    ) -> None:
        try:
            trace.append(ToolCallTrace(
                name=name or result.tool_name,
                args=args if isinstance(args, dict) else {},
                success=result.success,
                short_result=result.message or "",
                reason=reason,
            ))
        except ValidationError:  # pragma: no cover - defensive
            logger.error("could not record trace for %s", name)

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return current.astimezone(IST)

    def _system_prompt(self) -> str:
        context = self._state.get("context") or {}
        return build_system_prompt(
            self._now(),
            student_name=context.get("student_name"),
            prefs=context.get("prefs") or {},
        )

    def _guard_saved_claim(self, reply: str,
                           trace: list[ToolCallTrace]) -> str:
        """Text alone can never assert a save: require a successful tool call."""
        saved_for_real = any(
            call.name == "save_plan" and call.success for call in trace
        )
        if saved_for_real:
            return reply
        lowered = reply.lower()
        if any(phrase in lowered for phrase in _SAVED_NEGATIONS):
            return reply
        if not _SAVED_CLAIM_RE.search(reply):
            return reply
        if self._state.get("pending"):
            return (
                reply.rstrip()
                + "\n\n(Heads-up: it is not saved yet — press Confirm and I'll save it.)"
            )
        return (
            reply.rstrip()
            + "\n\n(Heads-up: nothing was saved. Ask me to plan first, then "
            "confirm the proposal.)"
        )

    def _normalise_state(self, state: Mapping[str, Any] | None) -> dict[str, Any]:
        if state is None:
            return _empty_state()
        try:
            clean = json.loads(json.dumps(dict(state)))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "agent state must be JSON-serializable (store export_state())"
            ) from exc
        base = _empty_state()
        for key in base:
            if key in clean:
                base[key] = clean[key]
        if not isinstance(base["history"], list):
            base["history"] = []
        if not isinstance(base["cache"], dict):
            base["cache"] = {}
        if not isinstance(base["context"], dict):
            base["context"] = {}
        if not isinstance(base["seen_ids"], list):
            base["seen_ids"] = []
        if base["pending"] is not None and not isinstance(base["pending"], dict):
            base["pending"] = None
        base["version"] = _STATE_VERSION
        return base


def _call_key(name: str, args: Any) -> str:
    try:
        return json.dumps(
            {"name": name, "args": args}, sort_keys=True, default=str
        )
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return f"{name}:{args!r}"


def _trim_payload(node: Any, *, max_items: int = 20,
                  max_string: int = 500) -> Any:
    """Keep IDs and summaries; truncate the bulk before it reaches the model."""
    if isinstance(node, list):
        trimmed = [_trim_payload(item) for item in node[:max_items]]
        if len(node) > max_items:
            trimmed.append(f"... {len(node) - max_items} more item(s) omitted")
        return trimmed
    if isinstance(node, dict):
        # Student IDs are injected server-side; the model never needs to see them.
        return {
            str(key): _trim_payload(value)
            for key, value in node.items()
            if str(key) != "student_id"
        }
    if isinstance(node, str) and len(node) > max_string:
        return node[:max_string] + "…"
    return node
