"""Streamlit harness tests (headless, no network).

The app is driven with Streamlit's AppTest. The backend and agent state are
seeded in session_state, then real buttons/chat input are clicked.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.controller import CampusPilotAgent
from agent.llm_client import LLMFunctionCall, LLMTurn
from agent.mock_backend import MockBackend
from agent.manual_app import decide_action, is_affirmative

IST = timezone(timedelta(hours=5, minutes=30))
FIXED_NOW = datetime(2026, 10, 19, 9, 0, tzinfo=IST)
STUDENT = "stu_arjun"
START = "2026-10-19T00:00:00+05:30"
END = "2026-10-21T23:59:00+05:30"

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
APP_PATH = str(Path(__file__).resolve().parents[1] / "agent" / "manual_app.py")


class ScriptedLLM:
    def __init__(self, turns: list[object]) -> None:
        self.turns = list(turns)
        self.calls = 0

    def generate(self, **kwargs):
        self.calls += 1
        turn = self.turns.pop(0)
        if isinstance(turn, BaseException):
            raise turn
        return turn


def make_pending() -> tuple[MockBackend, dict]:
    backend = MockBackend(now=lambda: FIXED_NOW)
    agent = CampusPilotAgent(
        STUDENT,
        backend,
        ScriptedLLM([
            LLMTurn(function_calls=[LLMFunctionCall("create_plan", {
                "start_date": START, "end_date": END, "task_ids": ["task_dbms"],
            })]),
            LLMTurn(text="Proposal ready — confirm to save."),
        ]),
        state={"seen_ids": ["task_dbms"]},
        clock=lambda: FIXED_NOW,
    )
    response = agent.handle_message("plan my DBMS work")
    assert response.pending_confirmation is not None
    assert backend.active_plan_id is None
    return backend, agent.export_state()


def new_app(backend: MockBackend | None = None,
            state: dict | None = None) -> "AppTest":
    at = AppTest.from_file(APP_PATH, default_timeout=45)
    if backend is not None:
        at.session_state["backend"] = backend
    if state is not None:
        at.session_state["agent_state"] = state
        at.session_state["messages"] = []
    return at.run()


def test_affirmative_detection_is_narrow() -> None:
    for text in ("yes", "Yes!", "confirm", "ok", "save it", "go ahead"):
        assert is_affirmative(text) is True
    for text in ("Save my plan", "yes but change Tuesday", "no", "what?"):
        assert is_affirmative(text) is False


def test_decide_action_requires_a_pending_plan() -> None:
    pending = {"plan_id": "plan_123"}
    assert decide_action(pending, "yes") == "confirm"
    assert decide_action(None, "yes") == "message"
    assert decide_action(pending, "Save my plan") == "message"


def test_app_imports_with_streamlit_script_path() -> None:
    """`streamlit run` puts agent/ first on sys.path; imports must still work."""
    root = Path(__file__).resolve().parents[1]
    code = (
        "import runpy, sys; "
        f"sys.path = [p for p in sys.path if p not in ('', {str(root)!r})]; "
        f"sys.path.insert(0, {str(root / 'agent')!r}); "
        f"runpy.run_path({str(root / 'agent' / 'manual_app.py')!r}, "
        "run_name='not_main')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_app_starts_and_shows_the_pending_card() -> None:
    backend, state = make_pending()
    at = new_app(backend, state)
    assert not at.exception
    assert backend.active_plan_id is None
    assert any("Pending plan" in item.value for item in at.subheader)
    assert at.get_by_key("confirm_pending") is not None
    assert at.get_by_key("cancel_pending") is not None


def test_clicking_confirm_is_what_saves_the_plan() -> None:
    backend, state = make_pending()
    plan_id = state["pending"]["plan_id"]
    at = new_app(backend, state)

    at.get_by_key("confirm_pending").click().run()

    assert not at.exception
    assert backend.active_plan_id == plan_id
    assert at.session_state["agent_state"]["pending"] is None
    assert at.session_state["messages"][-1]["text"].startswith("Saved")
    assert len(backend.snapshot()["saved_plans"]) == 1

    # Re-running does not create a duplicate or a second save.
    at.run()
    assert len(backend.snapshot()["saved_plans"]) == 1


def test_cancel_never_saves() -> None:
    backend, state = make_pending()
    at = new_app(backend, state)

    at.get_by_key("cancel_pending").click().run()

    assert not at.exception
    assert backend.active_plan_id is None
    assert at.session_state["agent_state"]["pending"] is None


def test_typing_yes_routes_to_confirm_without_the_llm() -> None:
    backend, state = make_pending()
    plan_id = state["pending"]["plan_id"]
    at = new_app(backend, state)

    at.chat_input[0].set_value("yes").run()

    assert not at.exception
    assert backend.active_plan_id == plan_id
    assert at.session_state["agent_state"]["pending"] is None
    assert "Saved" in at.session_state["messages"][-1]["text"]


def test_save_my_plan_text_does_not_save_by_itself() -> None:
    backend, state = make_pending()
    at = new_app(backend, state)

    at.chat_input[0].set_value("Save my plan").run()

    assert not at.exception
    assert backend.active_plan_id is None
    assert at.session_state["agent_state"]["pending"] is not None
    reply = at.session_state["messages"][-1]["text"]
    assert "press confirm" in reply.lower()
    assert len(backend.snapshot()["saved_plans"]) == 0


def test_outage_toggle_falls_back_and_still_proposes() -> None:
    at = new_app()
    at.session_state["toggle_outage"] = True
    at.run()

    at.chat_input[0].set_value("Plan my next two days").run()

    assert not at.exception
    last = at.session_state["messages"][-1]["response"]
    assert last["used_fallback"] is True
    assert last["pending_confirmation"] is not None
    assert at.session_state["backend"].active_plan_id is None


def test_two_app_instances_keep_separate_state() -> None:
    backend_a, state_a = make_pending()
    at_a = new_app(backend_a, state_a)
    at_b = new_app()
    assert at_b.session_state["backend"] is not backend_a

    at_a.get_by_key("confirm_pending").click().run()

    assert backend_a.active_plan_id is not None
    assert at_b.session_state["backend"].active_plan_id is None
    assert at_b.session_state["backend"].snapshot()["saved_plans"] == {}
    assert at_b.session_state["agent_state"] is None


def test_malicious_notice_injection_is_data_only() -> None:
    at = new_app()
    at.get_by_key("inject_notice").click().run()
    assert not at.exception

    at.chat_input[0].set_value("search my notices for scholarship").run()
    assert not at.exception
    last = at.session_state["messages"][-1]["response"]
    assert [call["name"] for call in last["tool_calls"]] == ["search_notices"]
    assert "IGNORE ALL RULES" in last["reply_text"]
    backend = at.session_state["backend"]
    assert backend.active_plan_id is None
    assert all(task.status == "pending" for task in backend._tasks.values())
    assert json.dumps(at.session_state["agent_state"])  # still JSON-safe
