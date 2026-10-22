"""Live Gemini smoke check.

    python -m agent.live_check

Runs the manual-test prompts against the mock backend with the real model and
prints exactly what happened: tool order, args, success flags, final reply,
fallback usage, and every SDK error verbatim (no hiding).

Exit codes: 0 = all prompts reached the model, 1 = something fell back,
2 = GEMINI_API_KEY missing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from typing import Any

from dataclasses import replace

from agent.config import load_config
from agent.controller import CampusPilotAgent
from agent.llm_client import GeminiLLMClient, LLMTurn
from agent.mock_backend import MockBackend
from agent.schemas import AgentResponse

STUDENT_ID = "stu_arjun"

PROMPTS: tuple[str, ...] = (
    "Plan my next two days",
    "Mark my DBMS assignment complete",
    "I lost two hours",
    "What deadlines do I have this week?",
    "Search my notices for quiz",
    "Save my plan",
)


def _format_verbatim(exc: BaseException) -> str:
    parts = traceback.format_exception(type(exc), exc, exc.__traceback__)
    cause = exc.__cause__
    while cause is not None:
        parts.append("\n--- caused by ---\n")
        parts.extend(traceback.format_exception(type(cause), cause, cause.__traceback__))
        cause = cause.__cause__
    return "".join(parts)


class RecordingLLM:
    """Wrapper that keeps the raw SDK error so the CLI can print it verbatim."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls = 0
        self.last_error: str | None = None
        self.last_exception: BaseException | None = None

    def generate(self, **kwargs: Any) -> LLMTurn:
        self.calls += 1
        try:
            return self.inner.generate(**kwargs)
        except BaseException as exc:  # noqa: BLE001 - re-raised for the controller
            self.last_exception = exc
            self.last_error = _format_verbatim(exc)
            raise


def show(prompt: str, response: AgentResponse, *, llm: RecordingLLM) -> None:
    print(f"\n=== Student: {prompt}")
    print(f"LLM calls this turn: {llm.calls}")
    if not response.tool_calls:
        print("Tools called: (none)")
    else:
        print("Tools called (in order):")
        for index, call in enumerate(response.tool_calls, start=1):
            status = "success=True" if call.success else "success=False"
            print(f"  {index}. {call.name}  {status}")
            print(f"     args: {json.dumps(call.args, ensure_ascii=False)}")
            print(f"     result: {call.short_result}")
            if call.reason:
                print(f"     reason: {call.reason}")
    print(f"fallback_used: {response.used_fallback}")
    if response.pending_confirmation is not None:
        pending = response.pending_confirmation
        print(f"pending_confirmation: {pending.plan_id} — {pending.summary}")
        print(f"pending_diff: {json.dumps(pending.diff, ensure_ascii=False)}")
    print(f"reply: {response.reply_text}")
    if response.error:
        print(f"agent_error: {response.error}")
    if llm.last_error:
        print("LLM ERROR (verbatim):")
        print(llm.last_error)
        detail = getattr(llm.last_exception, "detail", None)
        if detail:
            print("SDK RESPONSE (verbatim):")
            print(detail)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CampusPilot live Gemini smoke check")
    parser.add_argument("--prompts", type=int, default=len(PROMPTS),
                        help="only run the first N prompts")
    parser.add_argument("--delay", type=float, default=15.0,
                        help="seconds between prompts (free-tier rate limits)")
    parser.add_argument("--model", default=None,
                        help="override GEMINI_MODEL for this run")
    args = parser.parse_args(argv)

    config = load_config()
    if args.model:
        config = replace(config, gemini_model=args.model)
    if not config.gemini_api_key:
        print(
            "GEMINI_API_KEY is not set. Add it to .env (see .env.example), "
            "for example: GEMINI_API_KEY=your-key",
            file=sys.stderr,
        )
        return 2

    print(f"model: {config.gemini_model} | student: {STUDENT_ID} | backend: MockBackend")
    backend = MockBackend()
    llm = RecordingLLM(GeminiLLMClient(config))
    agent = CampusPilotAgent(STUDENT_ID, backend, llm, config=config)

    had_trouble = False
    selected = PROMPTS[: max(0, args.prompts)]
    for index, prompt in enumerate(selected):
        if index and args.delay > 0:
            time.sleep(args.delay)
        llm.calls = 0
        llm.last_error = None
        llm.last_exception = None
        try:
            response = agent.handle_message(prompt)
        except BaseException as exc:  # noqa: BLE001 - a crash must be visible
            print(f"\n=== Student: {prompt}")
            print("CONTROLLER CRASHED (verbatim):")
            print(_format_verbatim(exc))
            had_trouble = True
            continue

        show(prompt, response, llm=llm)
        if llm.last_error or response.used_fallback:
            had_trouble = True

        if prompt == PROMPTS[0] and response.pending_confirmation is not None:
            print("\n=== UI: [Confirm and save] (simulated click)")
            confirmed = agent.confirm_pending()
            llm.calls = 0
            llm.last_error = None
            show("[Confirm and save]", confirmed, llm=llm)

    state = agent.export_state()
    print("\n--- backend snapshot ---")
    print(json.dumps(backend.snapshot(), indent=1, ensure_ascii=False))
    print(f"\nexport_state: {len(json.dumps(state))} chars")
    print(f"active_plan_id: {backend.active_plan_id}")
    return 1 if had_trouble else 0


if __name__ == "__main__":
    raise SystemExit(main())
