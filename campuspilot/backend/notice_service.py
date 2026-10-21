
"""Campus notice services for CampusPilot."""

from backend.database import get_connection, transaction
from backend.ids import new_id
from backend.timeutil import now_ist, to_iso


def create_notice(
    *,
    title: str,
    body: str,
    category: str | None = None,
    student_id: str | None = None,
) -> str:
    """Create a campus notice, optionally for one student."""

    if not title.strip():
        raise ValueError("Notice title cannot be empty.")

    if not body.strip():
        raise ValueError("Notice body cannot be empty.")

    if student_id is not None and not student_id.strip():
        raise ValueError("student_id cannot be empty.")

    notice_id = new_id("notice")
    posted_at = to_iso(now_ist())

    with get_connection() as connection:
        with transaction(connection):
            connection.execute(
                """
                INSERT INTO notices (
                    notice_id, student_id, title, body,
                    category, posted_at, is_read
                )
                VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    notice_id,
                    student_id,
                    title.strip(),
                    body.strip(),
                    category.strip() if category and category.strip() else None,
                    posted_at,
                ),
            )

    return notice_id


def search_notices(
    *,
    student_id: str,
    query: str,
    category: str | None = None,
) -> list[dict]:
    """Search campus-wide and student-specific notices."""

    if not student_id.strip():
        raise ValueError("student_id cannot be empty.")

    if not query.strip():
        raise ValueError("Search query cannot be empty.")

    search_term = f"%{query.strip()}%"

    sql = """
        SELECT notice_id, student_id, title, body,
               category, posted_at, is_read
        FROM notices
        WHERE (student_id IS NULL OR student_id = ?)
          AND (
              title LIKE ? COLLATE NOCASE
              OR body LIKE ? COLLATE NOCASE
          )
    """
    parameters: list[object] = [
        student_id,
        search_term,
        search_term,
    ]

    if category is not None and category.strip():
        sql += " AND category = ? COLLATE NOCASE"
        parameters.append(category.strip())

    sql += " ORDER BY posted_at DESC"

    with get_connection() as connection:
        rows = connection.execute(sql, parameters).fetchall()

    return [dict(row) for row in rows]
