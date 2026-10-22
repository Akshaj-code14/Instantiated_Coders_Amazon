"""End-to-end journey against the running FastAPI stack (offline LLM)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from services.database import get_connection


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMPUSPILOT_DB", str(tmp_path / "e2e.db"))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    import server as server_module

    server_module.registry.reset()
    return TestClient(server_module.app), server_module


def test_lost_time_replan_preserves_and_persists(tmp_path, monkeypatch):
    client, server_module = _client(tmp_path, monkeypatch)
    with client:
        plan = client.post(
            "/chat", json={"message": "Plan my next two days"}
        ).json()["proposed_plan"]
        assert plan
        saved = client.post("/plan/save", json={"plan": plan})
        assert saved.status_code == 200, saved.text

        state = client.get("/state").json()
        active = state["active_plan"]
        assert active and active["plan_id"] == plan["plan_id"]

        scheduled = [item for item in active["items"] if item["status"] == "scheduled"]
        assert scheduled
        target = next(item for item in scheduled if item["task_id"])
        old_total = sum(item["minutes"] for item in scheduled)

        completed = client.post(
            "/tasks/complete",
            json={"task_id": target["task_id"], "done": True},
        )
        assert completed.status_code == 200

        with get_connection() as connection:
            item_status = connection.execute(
                """
                SELECT status FROM plan_items
                WHERE plan_id = ? AND task_id = ?
                """,
                (active["plan_id"], target["task_id"]),
            ).fetchone()["status"]
        assert item_status == "completed"

        lost = client.post(
            "/chat", json={"message": "I lost two hours"}
        ).json()
        proposal = lost["proposed_plan"]
        assert proposal is not None
        assert proposal["plan_id"] != active["plan_id"]
        assert set(proposal["diff"]) >= {"added", "moved", "removed", "unchanged"}
        assert proposal["diff"]["added"] or proposal["diff"]["moved"] or proposal["diff"]["removed"]

        proposed_minutes = sum(
            item["minutes"]
            for item in proposal["items"]
            if item["status"] == "proposed"
        )
        assert proposed_minutes <= max(0, old_total - 120)

        # The old plan is untouched while the replan is only proposed.
        state = client.get("/state").json()
        assert state["active_plan"]["plan_id"] == active["plan_id"]
        assert state["pending_plan"]["plan_id"] == proposal["plan_id"]
        with get_connection() as connection:
            old_items = connection.execute(
                "SELECT status FROM plan_items WHERE plan_id = ?",
                (active["plan_id"],),
            ).fetchall()
        assert any(item["status"] == "completed" for item in old_items)

        confirmed = client.post("/plan/save", json={"plan_id": proposal["plan_id"]})
        assert confirmed.status_code == 200, confirmed.text

        state = client.get("/state").json()
        assert state["active_plan"]["plan_id"] == proposal["plan_id"]
        assert state["pending_plan"] is None
        assert any(
            item["task_id"] == target["task_id"] and item["status"] == "completed"
            for item in state["active_plan"]["items"]
        )

        checkin = client.post(
            "/checkin", json={"status": "Completed", "mood": "Okay"}
        )
        assert checkin.status_code == 200

        # Simulate a server restart: the registry is empty, the browser cookie
        # remains, and the agent must rebuild active/pending state from SQLite.
        server_module.registry.reset()
        after = client.get("/state").json()
        assert after["active_plan"]["plan_id"] == proposal["plan_id"]
        assert after["checkin"]["completion_status"] == "completed"
        assert any(
            item["task_id"] == target["task_id"] and item["status"] == "completed"
            for item in after["active_plan"]["items"]
        )
