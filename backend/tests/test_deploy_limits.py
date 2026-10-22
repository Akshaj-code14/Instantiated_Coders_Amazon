"""Deploy-time behavior (offline): budget caps, throttles, gate, reset.

Every test here runs against the real FastAPI app with a temporary SQLite file
and a fake Gemini SDK client, so nothing touches the network.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent.config import AgentConfig
from agent.live_budget import budget, client_ip_from_headers
from agent.llm_client import GeminiLLMClient, LiveLimitReached


class _FakeModels:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def generate_content(self, **_: object):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _response(text: str = "Fine."):
    part = SimpleNamespace(
        text=text, function_call=None, thought=False, thought_signature=None
    )
    return SimpleNamespace(
        candidates=[SimpleNamespace(
            content=SimpleNamespace(parts=[part]), finish_reason="STOP"
        )],
        prompt_feedback=None,
    )


def _llm(outcomes: list[object], *, retries: int = 0):
    sdk = SimpleNamespace(models=_FakeModels(outcomes))
    client = GeminiLLMClient(
        AgentConfig(gemini_api_key="fake-key", max_retries=retries),
        client=sdk,
        sleep=lambda _: None,
    )
    return client, sdk


def _generate(client: GeminiLLMClient):
    return client.generate(
        system_prompt="system",
        history=[{"role": "user", "text": "plan my next two days"}],
        tools=[],
    )


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMPUSPILOT_DB", str(tmp_path / "deploy.db"))
    monkeypatch.setenv("GEMINI_API_KEY", "")

    import server as server_module

    server_module.registry.reset()
    with TestClient(server_module.app) as client:
        yield client, server_module
    server_module.registry.reset()


# ---------------------------------------------------------------------------
# Item 3 — live-AI budget
# ---------------------------------------------------------------------------


def test_default_caps_when_env_is_unset(monkeypatch):
    monkeypatch.delenv("LIVE_CALLS_PER_DAY", raising=False)
    monkeypatch.delenv("LIVE_CALLS_PER_IP_PER_HOUR", raising=False)
    assert budget.limits() == (15, 5)


def test_client_ip_prefers_the_proxy_header():
    assert client_ip_from_headers(
        "10.0.0.1", "203.0.113.9, 10.0.0.1"
    ) == "203.0.113.9"
    assert client_ip_from_headers("10.0.0.1", None) == "10.0.0.1"
    assert client_ip_from_headers(None, None) == "unknown"


def test_daily_cap_counts_every_request_including_retries(monkeypatch):
    from google.genai import errors

    monkeypatch.setenv("LIVE_CALLS_PER_DAY", "2")
    monkeypatch.setenv("LIVE_CALLS_PER_IP_PER_HOUR", "100")

    first, sdk = _llm(
        [errors.ClientError(503, {"error": {"message": "busy"}}), _response()],
        retries=1,
    )
    assert _generate(first).text == "Fine."
    assert sdk.models.calls == 2, "the retry must be charged as a real request"
    assert budget.remaining() == 0

    second, sdk_second = _llm([_response("never sent")])
    with pytest.raises(LiveLimitReached):
        _generate(second)
    assert sdk_second.models.calls == 0, "a capped turn must not hit Gemini"


def test_per_ip_hourly_cap_is_separate_from_the_global_one(monkeypatch):
    monkeypatch.setenv("LIVE_CALLS_PER_DAY", "100")
    monkeypatch.setenv("LIVE_CALLS_PER_IP_PER_HOUR", "1")

    assert budget.acquire("203.0.113.5") is None
    assert budget.acquire("203.0.113.5") == "per_ip"
    assert budget.acquire("198.51.100.9") is None, "another visitor keeps its own quota"


def test_no_key_means_offline_without_errors(app_client):
    client, _ = app_client

    health = client.get("/health").json()
    assert health["llm"] == "offline"
    assert isinstance(health["live_calls_remaining"], int)

    chat = client.post("/chat", json={"message": "Plan my next two days"})
    assert chat.status_code == 200
    body = chat.json()
    assert body["used_fallback"] is True
    assert "no GEMINI_API_KEY" in body["notice"]
    assert body["proposed_plan"]["items"]


def test_cap_reached_routes_to_fallback_with_the_friendly_label(
    app_client, monkeypatch
):
    client, _ = app_client
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    monkeypatch.setenv("LIVE_CALLS_PER_DAY", "1")
    monkeypatch.setenv("LIVE_CALLS_PER_IP_PER_HOUR", "100")

    assert budget.acquire("some-other-visitor") is None  # spend the only call

    health = client.get("/health").json()
    assert health["llm"] == "capped"
    assert health["live_calls_remaining"] == 0

    chat = client.post("/chat", json={"message": "Plan my next two days"})
    assert chat.status_code == 200, chat.text
    body = chat.json()
    assert body["used_fallback"] is True
    assert body["notice"] == "Live AI limit reached; using fallback mode."
    assert body["reply"].strip()
    assert body["proposed_plan"]["items"]


def test_health_never_leaks_the_key(app_client, monkeypatch):
    client, _ = app_client
    secret = "AIzaSyFAKE-test-key-value-1234567890"
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    monkeypatch.setenv("LIVE_CALLS_PER_DAY", "15")

    response = client.get("/health")
    assert response.status_code == 200
    assert secret not in response.text
    assert "AIza" not in response.text
    assert "key" not in response.text.lower()
    assert response.json()["live_calls_remaining"] == 15

    assert secret not in client.get("/state").text


# ---------------------------------------------------------------------------
# Item 4 — abuse limits
# ---------------------------------------------------------------------------


def test_chat_is_rate_limited_per_visitor(app_client):
    client, _ = app_client
    for index in range(10):
        assert client.post(
            "/chat", json={"message": f"hello {index}"}
        ).status_code == 200

    blocked = client.post("/chat", json={"message": "one too many"})
    assert blocked.status_code == 429
    assert "faster than the demo allows" in blocked.json()["error"]


def test_message_and_body_limits_are_friendly(app_client):
    client, _ = app_client

    too_long = client.post("/chat", json={"message": "x" * 2001})
    assert too_long.status_code == 422
    assert too_long.json()["error"].startswith("Invalid request")

    huge = client.post(
        "/chat",
        content=b"{" + b"x" * 20_000,
        headers={"Content-Type": "application/json"},
    )
    assert huge.status_code == 413
    assert huge.json()["error"] == "That request is too large."


def test_security_headers_and_same_origin_guard(app_client):
    client, _ = app_client

    root = client.get("/")
    assert root.headers["x-content-type-options"] == "nosniff"
    assert root.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert root.headers["x-frame-options"] == "DENY"

    blocked = client.post(
        "/chat",
        json={"message": "hello"},
        headers={"Origin": "https://evil.example"},
    )
    assert blocked.status_code == 403
    assert blocked.json()["error"] == "Cross-origin request blocked."


def test_optional_password_gate(app_client, monkeypatch):
    client, server_module = app_client
    monkeypatch.setattr(server_module, "DEMO_PASSWORD", "open-sesame")

    assert client.get("/health").status_code == 200, "/health stays open"
    assert client.get("/").status_code == 401
    assert "password" in client.get("/").text.lower()

    opened = client.get("/?demo_password=open-sesame")
    assert opened.status_code == 200
    assert "CampusPilot" in opened.text
    assert opened.cookies.get("campuspilot_demo"), "the gate sets its cookie"

    assert client.get("/state").status_code == 200, "cookie authorises the API"
    wrong = client.get("/state", headers={"X-Demo-Password": "wrong"})
    assert wrong.status_code == 401


# ---------------------------------------------------------------------------
# Items 2 and 5 — startup seed and demo reset
# ---------------------------------------------------------------------------


def test_empty_database_file_is_seeded_on_startup(tmp_path, monkeypatch):
    db_path = tmp_path / "empty.db"
    db_path.write_bytes(b"")  # present but empty, like a fresh volume
    monkeypatch.setenv("CAMPUSPILOT_DB", str(db_path))
    monkeypatch.setenv("GEMINI_API_KEY", "")

    import server as server_module

    server_module.registry.reset()
    with TestClient(server_module.app) as client:
        state = client.get("/state").json()

    assert state["profile"]["name"] == "Aditya Verma"
    assert len(state["tasks"]) == 6
    assert state["active_plan"] is None


def test_demo_reset_reseeds_and_clears_plans(app_client):
    client, _ = app_client
    proposal = client.post(
        "/chat", json={"message": "Plan my next two days"}
    ).json()["proposed_plan"]
    assert client.post("/plan/save", json={"plan": proposal}).status_code == 200
    assert client.get("/state").json()["active_plan"]

    assert client.post("/demo/reset", json={}).status_code == 200

    state = client.get("/state").json()
    assert state["active_plan"] is None
    assert state["pending_plan"] is None
    assert len(state["tasks"]) == 6
    assert state["counts"]["completed"] == 1


def test_demo_reset_is_rate_limited(app_client):
    client, _ = app_client
    for _ in range(3):
        assert client.post("/demo/reset", json={}).status_code == 200
    blocked = client.post("/demo/reset", json={})
    assert blocked.status_code == 429
    assert "reset recently" in blocked.json()["error"]


def test_plan_confirm_survives_a_restart(app_client):
    client, server_module = app_client
    proposal = client.post(
        "/chat", json={"message": "Plan my next two days"}
    ).json()["proposed_plan"]
    saved = client.post("/plan/save", json={"plan": proposal})
    assert saved.status_code == 200
    plan_id = saved.json()["plan_id"]

    # A restart forgets every in-memory session; SQLite must still have it.
    server_module.registry.reset()
    state = client.get("/state").json()
    assert state["active_plan"]["plan_id"] == plan_id
    assert state["pending_plan"] is None
