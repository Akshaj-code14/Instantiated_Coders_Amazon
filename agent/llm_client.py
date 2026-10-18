"""Thin, defensive wrapper around the Gemini SDK.

Manual function calling only: the SDK never executes a tool. Every SDK
exception is mapped to :class:`LLMUnavailableError`, so the controller (and the
UI) never sees a raw ``google.genai`` or ``httpx`` exception.

The public surface is one method — ``generate`` — which makes it trivial to
swap in a scripted fake in tests.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

import httpx
from google import genai
from google.genai import errors, types

from agent.config import AgentConfig, load_config

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
_SAFETY_FINISH_REASONS = frozenset(
    {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "RECITATION"}
)
_FRIENDLY_UNAVAILABLE = (
    "I can't reach my AI brain right now. I can still handle the quick actions "
    "below, or you can rephrase in a moment."
)


class LLMUnavailableError(RuntimeError):
    """Raised for every LLM failure: network, quota, safety, empty response."""

    def __init__(self, message: str, *, retryable: bool = False,
                 cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.cause = cause
        self.user_message = _FRIENDLY_UNAVAILABLE


@dataclass(frozen=True)
class LLMFunctionCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    # Gemini 2.5 thinking models require the original thought signature when a
    # function call is replayed. Stored as base64 text so history stays JSON-safe.
    thought_signature: str | None = None


@dataclass(frozen=True)
class LLMTurn:
    text: str = ""
    function_calls: list[LLMFunctionCall] = field(default_factory=list)

    @property
    def has_calls(self) -> bool:
        return bool(self.function_calls)


class LLMClient(Protocol):
    """One method, so tests can pass a scripted fake."""

    def generate(
        self,
        *,
        system_prompt: str,
        history: Sequence[Mapping[str, Any]],
        tools: Sequence[types.Tool],
    ) -> LLMTurn: ...


# ---------------------------------------------------------------------------
# History <-> SDK content conversion (history itself stays JSON-serializable)
# ---------------------------------------------------------------------------


def _to_plain(value: Any) -> Any:
    """Convert proto Map/Struct-ish values into plain dicts/lists."""
    if isinstance(value, Mapping):
        return {str(key): _to_plain(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(item) for item in value]
    return value


def history_to_contents(history: Sequence[Mapping[str, Any]]) -> list[types.Content]:
    contents: list[types.Content] = []
    for message in history:
        role = message.get("role")
        if role == "user":
            contents.append(types.Content(
                role="user",
                parts=[types.Part.from_text(text=str(message.get("text", "")))],
            ))
        elif role == "model":
            parts: list[types.Part] = []
            text = message.get("text") or ""
            if text:
                parts.append(types.Part.from_text(text=str(text)))
            for call in message.get("function_calls", []) or []:
                signature = call.get("thought_signature")
                signature_bytes = None
                if signature:
                    try:
                        signature_bytes = base64.b64decode(signature)
                    except (ValueError, TypeError):
                        logger.warning("dropping unreadable thought_signature")
                parts.append(types.Part(
                    function_call=types.FunctionCall(
                        name=str(call.get("name", "")),
                        args=_to_plain(call.get("args") or {}),
                    ),
                    thought_signature=signature_bytes,
                ))
            if not parts:
                continue
            contents.append(types.Content(role="model", parts=parts))
        elif role == "tool":
            # Gemini expects function responses in a *user* turn; this mirrors
            # what the SDK's own automatic function calling does.
            parts = []
            for result in message.get("results", []) or []:
                parts.append(types.Part.from_function_response(
                    name=str(result.get("name", "")),
                    response={"result": _to_plain(result.get("response") or {})},
                ))
            if parts:
                contents.append(types.Content(role="user", parts=parts))
        else:
            logger.warning("skipping history message with unknown role %r", role)
    return contents


def _encode_signature(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, str):
        return base64.b64encode(value.encode("utf-8")).decode("ascii")
    return None


# ---------------------------------------------------------------------------
# Gemini implementation
# ---------------------------------------------------------------------------


class GeminiLLMClient:
    """Manual-function-calling Gemini client with retries and a hard timeout."""

    def __init__(
        self,
        config: AgentConfig | None = None,
        *,
        client: Any | None = None,
        sleep: Any | None = None,
    ) -> None:
        self.config = config or load_config()
        self._sleep = sleep or time.sleep
        self._client = client

    # -- client construction -------------------------------------------

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            api_key = self.config.require_api_key()
        except RuntimeError as exc:
            raise LLMUnavailableError(str(exc), cause=exc) from exc
        try:
            retry_options = types.HttpRetryOptions(attempts=1)
        except Exception:  # pragma: no cover - SDK version drift
            retry_options = None
        try:
            self._client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(
                    timeout=int(self.config.llm_timeout_s * 1000),
                    retry_options=retry_options,
                ),
            )
        except Exception as exc:  # pragma: no cover - bad credentials/config
            raise LLMUnavailableError(
                f"could not initialise the Gemini client: {type(exc).__name__}",
                cause=exc,
            ) from exc
        return self._client

    # -- main entry point ----------------------------------------------

    def generate(
        self,
        *,
        system_prompt: str,
        history: Sequence[Mapping[str, Any]],
        tools: Sequence[types.Tool],
    ) -> LLMTurn:
        client = self._get_client()
        request = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=list(tools),
            temperature=0.2,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        )
        contents = history_to_contents(history)
        attempts = max(1, self.config.max_retries + 1)
        last_error: LLMUnavailableError | None = None

        for attempt in range(attempts):
            try:
                response = client.models.generate_content(
                    model=self.config.gemini_model,
                    contents=contents,
                    config=request,
                )
                return self._parse_response(response)
            except LLMUnavailableError as exc:
                last_error = exc
                retryable = exc.retryable
            except errors.APIError as exc:
                status = getattr(exc, "code", None)
                retryable = status in RETRYABLE_STATUS or (
                    isinstance(status, int) and status >= 500
                )
                logger.warning(
                    "Gemini API error (status=%s, attempt=%s/%s)",
                    status, attempt + 1, attempts,
                )
                last_error = LLMUnavailableError(
                    f"Gemini API error {status}", retryable=retryable, cause=exc
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                retryable = True
                logger.warning(
                    "Gemini transport error (%s, attempt=%s/%s)",
                    type(exc).__name__, attempt + 1, attempts,
                )
                last_error = LLMUnavailableError(
                    "Gemini request timed out", retryable=True, cause=exc
                )
            except Exception as exc:  # noqa: BLE001 - nothing may leak upward
                logger.exception("unexpected Gemini client failure")
                raise LLMUnavailableError(
                    f"unexpected Gemini failure ({type(exc).__name__})", cause=exc
                ) from exc

            if not retryable or attempt == attempts - 1:
                break
            delay = min(0.5 * (2 ** attempt), 4.0)
            self._sleep(delay)

        assert last_error is not None
        raise last_error

    # -- response parsing ----------------------------------------------

    def _parse_response(self, response: Any) -> LLMTurn:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            feedback = getattr(response, "prompt_feedback", None)
            reason = getattr(feedback, "block_reason", None) if feedback else None
            logger.warning("Gemini returned no candidates (block_reason=%s)", reason)
            raise LLMUnavailableError(
                f"Gemini returned no candidates (block_reason={reason})",
                retryable=True,
            )

        candidate = candidates[0]
        finish_reason = str(getattr(candidate, "finish_reason", "") or "").upper()
        if any(tag in finish_reason for tag in _SAFETY_FINISH_REASONS):
            logger.warning("Gemini stopped for safety: %s", finish_reason)
            raise LLMUnavailableError(
                f"Gemini stopped for safety ({finish_reason})", retryable=False
            )

        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        texts: list[str] = []
        calls: list[LLMFunctionCall] = []
        for part in parts:
            if getattr(part, "thought", False):
                continue
            text = getattr(part, "text", None)
            if text:
                texts.append(str(text))
            function_call = getattr(part, "function_call", None)
            if function_call is None:
                continue
            name = getattr(function_call, "name", None)
            if not name:
                logger.warning("ignoring a function call without a name")
                continue
            calls.append(LLMFunctionCall(
                name=str(name),
                args=_to_plain(getattr(function_call, "args", None) or {}),
                thought_signature=_encode_signature(
                    getattr(part, "thought_signature", None)
                ),
            ))

        text = "".join(texts).strip()
        if not text and not calls:
            logger.warning("Gemini returned an empty turn")
            raise LLMUnavailableError("Gemini returned an empty response",
                                      retryable=True)
        return LLMTurn(text=text, function_calls=calls)
