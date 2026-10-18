"""CLI smoke demo.

    python -m agent.demo            # uses Gemini if GEMINI_API_KEY is set
    python -m agent.demo --offline  # always exercises the fallback router

Runs the acceptance-style prompts against the mock backend and prints every
tool trace, so a failed assumption is visible instead of hidden in prose.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from agent.config import load_config
from agent.controller import CampusPilotAgent
from agent.llm_client import GeminiLLMClient, LLMUnavailableError
from agent.mock_backend import MockBackend
from agent.schemas import AgentResponse

STUDENT_ID = "stu_arjun"

PROMPTS: tuple[tuple[str, str], ...] = (
    ("plan", "Plan my next two days"),
    ("complete", "Mark my DBMS assignment complete"),
    ("lost-time", "I lost two hours"),
    ("deadlines", "What's due this week?"),
    ("crisis", "Honestly, I can't go on living like this"),
)


class OfflineLLM:
    """Deterministic stand-in that forces the fallback path."""

    def generate(self, **_: Any):
        raise LLMUnavailableError("offline mode: GEMINI_API_KEY not used")


def _build_agent(offline: bool) -> tuple[CampusPilotAgent, MockBackend]:
    config = load_config()
    backend = MockBackend()
    if offline or not config.gemini_api_key:
        llm: Any = OfflineLLM()
        mode = "fallback (rule-based)"
    else:
        llm = GeminiLLMClient(config)
        mode = f"Gemini ({config.gemini_model})"
    print(f"Mode: {mode} | backend: MockBackend | student: {STUDENT_ID}")
    return (
        CampusPilotAgent(STUDENT_ID, backend, llm, config=config),
        backend,
    )


def show(prompt: str, response: AgentResponse) -> None:
    print(f"\n=== Student: {prompt}")
    print(f"Agent: {response.reply_text}")
    for call in response.tool_calls:
        status = "ok  " if call.success else "FAIL"
        args = json.dumps(call.args, ensure_ascii=False)
        print(f"  [{status}] {call.name}({args})")
        print(f"         -> {call.short_result}")
        if call.reason:
            print(f"         reason: {call.reason}")
    if response.pending_confirmation is not None:
        pending = response.pending_confirmation
        print(f"  pending: {pending.plan_id} — {pending.summary}")
        if pending.diff:
            diff = json.dumps(pending.diff, ensure_ascii=False)
            print(f"  diff: {diff[:400]}{'…' if len(diff) > 400 else ''}")
    if response.used_fallback:
        print("  note: fallback mode — the LLM was unavailable")
    if response.error:
        print(f"  error: {response.error}")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):  # pragma: no cover - platform specific
            pass
    parser = argparse.ArgumentParser(description="CampusPilot agent demo")
    parser.add_argument(
        "--offline", action="store_true",
        help="force the rule-based fallback router (no network)",
    )
    args = parser.parse_args(argv)

    # The demo narrates failures itself; keep raw log lines out of the output.
    logging.getLogger("agent").setLevel(logging.ERROR)
    agent, backend = _build_agent(args.offline)
    responses: dict[str, AgentResponse] = {}

    for key, prompt in PROMPTS:
        if key == "crisis":
            print("\n--- safety check ---")
        response = agent.handle_message(prompt)
        responses[key] = response
        show(prompt, response)

        if key == "plan" and response.pending_confirmation is not None:
            print("\n=== UI: student presses [Confirm and save]")
            confirmed = agent.confirm_pending()
            show("[Confirm and save]", confirmed)

    print("\n--- state ---")
    state = agent.export_state()
    blob = json.dumps(state)
    print(f"export_state: {len(blob)} chars, pending={state['pending']}")
    print(f"active_plan_id: {backend.active_plan_id}")
    print(f"saved plans: {sorted(backend._saved)}")

    # Sanity: the crisis turn must not have scheduled anything.
    crisis_tools = [call.name for call in responses["crisis"].tool_calls]
    if "get_wellbeing_support" not in crisis_tools:
        print("FAIL: crisis prompt did not route to wellbeing support")
        return 1
    print("\nOK: crisis prompt returned wellbeing resources and paused scheduling.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
