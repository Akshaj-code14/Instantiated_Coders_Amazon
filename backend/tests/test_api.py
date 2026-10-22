"""HTTP-level API tests for the FastAPI server (offline)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from services.database import get_connection, transaction
from services.timeutil import now_ist, to_iso

STUDENT = "student_demo"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMPUSPILOT_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("GEMINI_API_KEY", "")

    import server as server_module

    server_module.registry.reset()
    with TestClient(server_module.app) as test_client:
        yield test_client
    server_module.registry.reset()


def _count(sql: str, params: tuple = ()) -> int:
    with get_connection() as connection:
        return connection.execute(sql, params).fetchone()[0]


def test_health_root_and_state(client):
    health = client.get("/health")
    assert health.status_code == 200
    # FIX (deploy-ready, item 3): /health now reports the live-AI budget and
    # must never expose the key (there is none in this offline suite anyway).
    body = health.json()
    assert body["status"] == "ok"
    assert body["llm"] == "offline"
    assert body["db"] == "ok"
    assert isinstance(body["live_calls_remaining"], int)
    assert body["live_calls_remaining"] >= 0

    root = client.get("/")
    assert root.status_code == 200
    assert "CampusPilot" in root.text

    state = client.get("/state")
    assert state.status_code == 200, state.text
    data = state.json()
    assert data["profile"]["name"] == "Aditya Verma"
    assert len(data["tasks"]) == 6
    assert data["counts"]["completed"] == 1
    assert data["llm"] == "offline"
    assert data["active_plan"] is None
    assert data["conflicts"], "the seeded DBMS deadline conflict must be surfaced"
    assert "DBMS Lab Submission" in data["conflicts"][0]


def test_chat_plan_proposes_without_saving(client):
    response = client.post(
        "/chat", json={"message": "Plan my next two days", "language": "en"}
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["proposed_plan"] is not None
    assert data["proposed_plan"]["items"]
    assert data["used_fallback"] is True
    assert data["notice"]
    assert any(call["name"] == "create_plan" for call in data["tool_calls"])
    assert _count(
        "SELECT COUNT(*) FROM plans WHERE student_id = ? AND status = 'active'",
        (STUDENT,),
    ) == 0


def test_save_is_server_side_and_guarded(client):
    # Nothing pending: reject.
    assert client.post("/plan/save", json={"plan": {}}).status_code == 409

    proposal = client.post(
        "/chat", json={"message": "Plan my next two days"}
    ).json()["proposed_plan"]
    assert proposal

    mismatch = client.post(
        "/plan/save", json={"plan_id": "plan_not_the_pending_one"}
    )
    assert mismatch.status_code == 400

    saved = client.post("/plan/save", json={"plan": proposal})
    assert saved.status_code == 200, saved.text
    assert saved.json()["plan_id"] == proposal["plan_id"]

    assert _count(
        "SELECT COUNT(*) FROM plans WHERE student_id = ? AND status = 'active'",
        (STUDENT,),
    ) == 1

    again = client.post("/plan/save", json={"plan": {}})
    assert again.status_code == 409
    assert _count(
        "SELECT COUNT(*) FROM plans WHERE student_id = ? AND status = 'active'",
        (STUDENT,),
    ) == 1


def test_cancel_discards_pending(client):
    proposal = client.post(
        "/chat", json={"message": "Plan my next two days"}
    ).json()["proposed_plan"]
    assert proposal
    cancelled = client.post("/plan/cancel", json={})
    assert cancelled.status_code == 200
    assert client.post("/plan/save", json={}).status_code == 409
    assert client.get("/state").json()["pending_plan"] is None


def test_cancel_keeps_an_existing_active_plan_intact(client):
    """FIX (item 4): cancelling a fresh proposal must not touch the plan
    that is already active."""
    first = client.post(
        "/chat", json={"message": "Plan my next two days"}
    ).json()["proposed_plan"]
    active_id = client.post("/plan/save", json={"plan": first}).json()["plan_id"]

    second = client.post(
        "/chat", json={"message": "Plan my next two days again"}
    ).json()["proposed_plan"]
    assert second and second["plan_id"] != active_id

    assert client.post("/plan/cancel", json={}).status_code == 200

    state = client.get("/state").json()
    assert state["active_plan"]["plan_id"] == active_id
    assert state["pending_plan"] is None
    assert _count(
        "SELECT COUNT(*) FROM plans WHERE student_id = ? AND status = 'active'",
        (STUDENT,),
    ) == 1


def test_save_chat_with_nothing_pending_is_honest(client):
    response = client.post("/chat", json={"message": "Save my plan"})
    assert response.status_code == 200
    assert "nothing to save" in response.json()["reply"].lower()
    assert _count("SELECT COUNT(*) FROM plans") == 0


def test_task_complete_postpone_duration_and_checkin(client):
    state = client.get("/state").json()
    task = next(item for item in state["tasks"] if item["status"] != "completed")

    completed = client.post(
        "/tasks/complete", json={"task_id": task["task_id"], "done": True}
    )
    assert completed.status_code == 200, completed.text
    state = client.get("/state").json()
    updated = next(
        item for item in state["tasks"] if item["task_id"] == task["task_id"]
    )
    assert updated["status"] == "completed"

    duration = client.post(
        "/tasks/duration", json={"task_id": task["task_id"], "minutes": 45}
    )
    assert duration.status_code == 200
    postponed = client.post(
        "/tasks/postpone", json={"task_id": task["task_id"]}
    )
    assert postponed.status_code == 200
    assert postponed.json()["status"] == "postponed"

    checkin = client.post(
        "/checkin", json={"status": "Partial", "mood": "Okay"}
    )
    assert checkin.status_code == 200, checkin.text
    latest = client.get("/state").json()["checkin"]
    assert latest["completion_status"] == "partial"
    assert latest["mood"] == "Okay"


def test_client_cannot_pass_extra_fields_or_foreign_tasks(client):
    invalid = client.post(
        "/chat",
        json={"message": "hello", "student_id": "student_other"},
    )
    assert invalid.status_code == 422
    assert "error" in invalid.json()

    with get_connection() as connection:
        with transaction(connection):
            connection.execute(
                """
                INSERT INTO students (student_id, name, email, created_at)
                VALUES ('student_other', 'Other', NULL, ?)
                """,
                (to_iso(now_ist()),),
            )
            connection.execute(
                """
                INSERT INTO tasks (
                    task_id, student_id, title, due_at, estimated_minutes,
                    priority, status, source, created_at, updated_at
                ) VALUES ('task_other', 'student_other', 'Other task',
                          ?, 60, 'high', 'pending', 'manual', ?, ?)
                """,
                (
                    to_iso(now_ist() + timedelta(days=1)),
                    to_iso(now_ist()),
                    to_iso(now_ist()),
                ),
            )

    foreign = client.post(
        "/tasks/complete", json={"task_id": "task_other", "done": True}
    )
    assert foreign.status_code == 404
    assert _count(
        "SELECT COUNT(*) FROM tasks WHERE task_id = 'task_other' AND status = 'pending'"
    ) == 1


def test_crisis_and_non_crisis_stress(client):
    crisis = client.post(
        "/chat", json={"message": "I can't go on anymore"}
    ).json()
    names = [call["name"] for call in crisis["tool_calls"]]
    assert names == ["get_wellbeing_support"]
    assert "112" in crisis["reply"] or "Tele-MANAS" in crisis["reply"]
    assert "create_plan" not in names

    stress = client.post(
        "/chat", json={"message": "this quiz is killing me"}
    ).json()
    assert "get_wellbeing_support" not in [
        call["name"] for call in stress["tool_calls"]
    ]


def test_adversarial_notice_is_plain_text(client):
    response = client.post(
        "/chat", json={"message": "show me the urgent notice"}
    ).json()
    assert "IGNORE ALL RULES" in response["reply"]
    names = [call["name"] for call in response["tool_calls"]]
    assert "search_notices" in names
    assert "update_task" not in names
    assert "create_task" not in names
    assert _count(
        "SELECT COUNT(*) FROM tasks WHERE student_id = ? AND status = 'pending'",
        (STUDENT,),
    ) == 5


def test_pending_and_active_survive_registry_restart(client):
    import server as server_module

    proposal = client.post(
        "/chat", json={"message": "Plan my next two days"}
    ).json()["proposed_plan"]
    assert proposal

    server_module.registry.reset()
    state = client.get("/state").json()
    assert state["pending_plan"]["plan_id"] == proposal["plan_id"]

    saved = client.post("/plan/save", json={"plan": proposal})
    assert saved.status_code == 200, saved.text

    state = client.get("/state").json()
    assert state["active_plan"]["plan_id"] == proposal["plan_id"]

    client.post("/checkin", json={"status": "Completed", "mood": "Okay"})
    server_module.registry.reset()

    state = client.get("/state").json()
    assert state["active_plan"]["plan_id"] == proposal["plan_id"]
    assert state["checkin"]["completion_status"] == "completed"
