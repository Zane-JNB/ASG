import sqlite3
from datetime import datetime, timezone

from scheduler.models import ProfileSettings

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_settings (
    student_id INTEGER PRIMARY KEY REFERENCES students(id),
    settings_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reflections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id),
    created_at TEXT NOT NULL,
    reflection_text TEXT NOT NULL,
    settings_before_json TEXT NOT NULL,
    settings_after_json TEXT NOT NULL,
    applied INTEGER NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    """Open (and initialize, if new) the database at path. ':memory:' works for tests."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_or_create_student(conn: sqlite3.Connection, name: str) -> int:
    """Return this student's id, creating a row (with default settings) if they're new."""
    row = conn.execute("SELECT id FROM students WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return row[0]

    cur = conn.execute(
        "INSERT INTO students (name, created_at) VALUES (?, ?)", (name, _now())
    )
    student_id = cur.lastrowid
    save_settings(conn, student_id, ProfileSettings())
    conn.commit()
    return student_id


def load_settings(conn: sqlite3.Connection, student_id: int) -> ProfileSettings:
    """Load a student's settings. Raises if the student doesn't exist yet."""
    row = conn.execute(
        "SELECT settings_json FROM profile_settings WHERE student_id = ?", (student_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no settings found for student_id={student_id}")
    return ProfileSettings.model_validate_json(row[0])


def save_settings(conn: sqlite3.Connection, student_id: int, settings: ProfileSettings) -> None:
    """Insert or overwrite a student's settings."""
    conn.execute(
        """
        INSERT INTO profile_settings (student_id, settings_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(student_id) DO UPDATE SET
            settings_json = excluded.settings_json,
            updated_at = excluded.updated_at
        """,
        (student_id, settings.model_dump_json(), _now()),
    )
    conn.commit()


def log_reflection(conn: sqlite3.Connection, student_id: int, reflection_text: str,
                   before: ProfileSettings, after: ProfileSettings, applied: bool) -> int:
    """Record a reflection and the settings snapshot before/after it. Returns the new row's id."""
    cur = conn.execute(
        """
        INSERT INTO reflections
            (student_id, created_at, reflection_text, settings_before_json, settings_after_json, applied)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (student_id, _now(), reflection_text,
         before.model_dump_json(), after.model_dump_json(), int(applied)),
    )
    conn.commit()
    return cur.lastrowid


def get_reflections(conn: sqlite3.Connection, student_id: int) -> list[dict]:
    """Return this student's reflection history, oldest first."""
    rows = conn.execute(
        """
        SELECT id, created_at, reflection_text, settings_before_json, settings_after_json, applied
        FROM reflections WHERE student_id = ? ORDER BY id ASC
        """,
        (student_id,),
    ).fetchall()
    return [
        {
            "id": r[0],
            "created_at": r[1],
            "reflection_text": r[2],
            "settings_before": ProfileSettings.model_validate_json(r[3]),
            "settings_after": ProfileSettings.model_validate_json(r[4]),
            "applied": bool(r[5]),
        }
        for r in rows
    ]