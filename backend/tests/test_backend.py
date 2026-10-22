
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path


class CampusPilotBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_campuspilot.db"

        self.previous_db = os.environ.get("CAMPUSPILOT_DB")
        os.environ["CAMPUSPILOT_DB"] = str(self.db_path)

        from services.database import get_connection, init_db, transaction
        from services.timeutil import now_ist, to_iso

        self.get_connection = get_connection
        self.transaction = transaction
        self.now_ist = now_ist
        self.to_iso = to_iso

        init_db()

        self.student_id = "student_test_001"

        with self.get_connection() as connection:
            with self.transaction(connection):
                connection.execute(
                    """
                    INSERT INTO students
                        (student_id, name, email, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        self.student_id,
                        "Automated Test Student",
                        "automated-test@example.com",
                        self.to_iso(self.now_ist()),
                    ),
                )

                for task_id, title in [
                    ("task_test_001", "Test Task One"),
                    ("task_test_002", "Test Task Two"),
                    ("task_test_003", "Fixed Event Test Task"),
                ]:
                    connection.execute(
                        """
                        INSERT INTO tasks
                            (task_id, student_id, title, due_at,
                             estimated_minutes, priority, status,
                             source, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            task_id,
                            self.student_id,
                            title,
                            None,
                            60,
                            "high",
                            "pending",
                            "manual",
                            self.to_iso(self.now_ist()),
                            self.to_iso(self.now_ist()),
                        ),
                    )

    def tearDown(self):
        if self.previous_db is None:
            os.environ.pop("CAMPUSPILOT_DB", None)
        else:
            os.environ["CAMPUSPILOT_DB"] = self.previous_db

        self.temp_dir.cleanup()

    def make_plan(self, task_id, start):
        from services.campuspilot_backend import create_plan

        result = create_plan(
            student_id=self.student_id,
            start_date=start,
            end_date=start + timedelta(days=1),
            task_ids=[task_id],
        )

        self.assertTrue(result["success"], result["message"])
        return result["data"]["plan_id"]

    def get_plan_status(self, plan_id):
        with self.get_connection() as connection:
            row = connection.execute(
                "SELECT status FROM plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()

        self.assertIsNotNone(row)
        return row["status"]

    def test_database_initializes(self):
        with self.get_connection() as connection:
            tables = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table'
                """
            ).fetchall()

        table_names = {row["name"] for row in tables}

        for table in ("students", "tasks", "plans", "plan_items"):
            self.assertIn(table, table_names)

    def test_backend_response_format(self):
        from services.campuspilot_backend import get_student_context

        result = get_student_context(
            student_id="student_does_not_exist"
        )

        self.assertIsInstance(result, dict)
        self.assertIn("success", result)
        self.assertEqual(result["tool_name"], "get_student_context")
        self.assertIn("data", result)
        self.assertIn("message", result)
        self.assertIn("requires_confirmation", result)

    def test_new_plan_stays_proposed_until_confirmation(self):
        start = (
            self.now_ist() + timedelta(days=1)
        ).replace(hour=9, minute=0, second=0, microsecond=0)

        plan_id = self.make_plan("task_test_001", start)

        self.assertEqual(self.get_plan_status(plan_id), "proposed")

    def test_confirmed_plan_becomes_active(self):
        from services.campuspilot_backend import save_plan

        start = (
            self.now_ist() + timedelta(days=1)
        ).replace(hour=9, minute=0, second=0, microsecond=0)

        plan_id = self.make_plan("task_test_001", start)

        result = save_plan(
            student_id=self.student_id,
            plan_id=plan_id,
            confirmed_by_user=True,
        )

        self.assertTrue(result["success"], result["message"])
        self.assertEqual(self.get_plan_status(plan_id), "active")

        with self.get_connection() as connection:
            items = connection.execute(
                """
                SELECT status FROM plan_items
                WHERE plan_id = ?
                """,
                (plan_id,),
            ).fetchall()

        self.assertTrue(items)
        self.assertTrue(all(item["status"] == "scheduled" for item in items))

    def test_confirming_new_plan_supersedes_previous_active_plan(self):
        from services.campuspilot_backend import save_plan

        start_one = (
            self.now_ist() + timedelta(days=1)
        ).replace(hour=9, minute=0, second=0, microsecond=0)

        first_plan = self.make_plan("task_test_001", start_one)

        first_result = save_plan(
            student_id=self.student_id,
            plan_id=first_plan,
            confirmed_by_user=True,
        )
        self.assertTrue(first_result["success"], first_result["message"])

        # Use a different day to avoid overlapping the active plan.
        start_two = start_one + timedelta(days=2)
        second_plan = self.make_plan("task_test_002", start_two)

        second_result = save_plan(
            student_id=self.student_id,
            plan_id=second_plan,
            confirmed_by_user=True,
        )
        self.assertTrue(second_result["success"], second_result["message"])

        self.assertEqual(self.get_plan_status(first_plan), "superseded")
        self.assertEqual(self.get_plan_status(second_plan), "active")

    def test_fixed_event_is_preserved_and_not_overlapped(self):
        from services.campuspilot_backend import create_plan

        start = (
            self.now_ist() + timedelta(days=1)
        ).replace(hour=9, minute=0, second=0, microsecond=0)

        event_start = start
        event_end = start + timedelta(hours=1)

        with self.get_connection() as connection:
            with self.transaction(connection):
                connection.execute(
                    """
                    INSERT INTO events
                        (event_id, student_id, title, start_at, end_at,
                         event_type, is_fixed, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "event_test_fixed",
                        self.student_id,
                        "Fixed Class",
                        self.to_iso(event_start),
                        self.to_iso(event_end),
                        "class",
                        1,
                        "manual",
                    ),
                )

        result = create_plan(
            student_id=self.student_id,
            start_date=start,
            end_date=start + timedelta(days=1),
            task_ids=["task_test_003"],
        )

        self.assertTrue(result["success"], result["message"])
        plan_id = result["data"]["plan_id"]

        with self.get_connection() as connection:
            event = connection.execute(
                """
                SELECT start_at, end_at, is_fixed
                FROM events
                WHERE event_id = ?
                """,
                ("event_test_fixed",),
            ).fetchone()

            items = connection.execute(
                """
                SELECT start_at, end_at
                FROM plan_items
                WHERE plan_id = ?
                """,
                (plan_id,),
            ).fetchall()

        self.assertEqual(event["start_at"], self.to_iso(event_start))
        self.assertEqual(event["end_at"], self.to_iso(event_end))
        self.assertEqual(event["is_fixed"], 1)

        for item in items:
            item_start = self.now_ist().fromisoformat(item["start_at"])
            item_end = self.now_ist().fromisoformat(item["end_at"])

            overlaps = item_start < event_end and item_end > event_start
            self.assertFalse(overlaps, "A proposed session overlaps a fixed event.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
