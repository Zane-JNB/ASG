import sqlite3
from datetime import datetime, timezone

from pydantic import BaseModel

from scheduler.models import ProfileSettings, FixedBlock, DynamicTask, Exam, WeeklyPattern, DatedBlock, ExtractedTask, ExtractionResult

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

CREATE TABLE IF NOT EXISTS fixed_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id),
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id),
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id),
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weekly_patterns ( 
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id),
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dated_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id),
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extracted_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id),
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
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

def _add_item(conn: sqlite3.Connection, table: str, student_id: int, item: BaseModel) -> int:
    """Insert one item (a FixedBlock, DynamicTask, or Exam) for a student. Returns its new id."""
    cur = conn.execute(
        f"INSERT INTO {table} (student_id, data_json, created_at) VALUES (?, ?, ?)",
        (student_id, item.model_dump_json(), _now()),
    )
    conn.commit()
    return cur.lastrowid

def _get_items(conn: sqlite3.Connection, table: str, student_id: int, model_cls: type) -> list:
    """Return every item of this type for a student, oldest first, as (id, model_instance) pairs."""
    rows = conn.execute(
        f"SELECT id, data_json FROM {table} WHERE student_id = ? ORDER BY id ASC", (student_id,)
    ).fetchall()
    return [(r[0], model_cls.model_validate_json(r[1])) for r in rows]

def _delete_item(conn: sqlite3.Connection, table: str, student_id: int, item_id: int) -> bool:
    """Delete one item by id, scoped to this student (so one student can't delete another's row)."""
    cur = conn.execute(
        f"DELETE FROM {table} WHERE id = ? AND student_id = ?", (item_id, student_id)
    )
    conn.commit()
    return cur.rowcount > 0

def _clear_items(conn: sqlite3.Connection, table: str, student_id: int) -> int:
    """Delete every item of this type for a student. Returns how many rows were removed."""
    cur = conn.execute(f"DELETE FROM {table} WHERE student_id = ?", (student_id,))
    conn.commit()
    return cur.rowcount

def add_fixed_block(conn: sqlite3.Connection, student_id: int, block: FixedBlock) -> int:
    return _add_item(conn, "fixed_blocks", student_id, block)

def get_fixed_blocks(conn: sqlite3.Connection, student_id: int) -> list[tuple[int, FixedBlock]]:
    return _get_items(conn, "fixed_blocks", student_id, FixedBlock)

def delete_fixed_block(conn: sqlite3.Connection, student_id: int, block_id: int) -> bool:
    return _delete_item(conn, "fixed_blocks", student_id, block_id)

def clear_fixed_blocks(conn: sqlite3.Connection, student_id: int) -> int:
    return _clear_items(conn, "fixed_blocks", student_id)

def add_task(conn: sqlite3.Connection, student_id: int, task: DynamicTask) -> int:
    return _add_item(conn, "tasks", student_id, task)

def get_tasks(conn: sqlite3.Connection, student_id: int) -> list[tuple[int, DynamicTask]]:
    return _get_items(conn, "tasks", student_id, DynamicTask)

def delete_task(conn: sqlite3.Connection, student_id: int, task_id: int) -> bool:
    return _delete_item(conn, "tasks", student_id, task_id)

def clear_tasks(conn: sqlite3.Connection, student_id: int) -> int:
    return _clear_items(conn, "tasks", student_id)

def add_exam(conn: sqlite3.Connection, student_id: int, exam: Exam) -> int:
    return _add_item(conn, "exams", student_id, exam)

def get_exams(conn: sqlite3.Connection, student_id: int) -> list[tuple[int, Exam]]:
    return _get_items(conn, "exams", student_id, Exam)

def delete_exam(conn: sqlite3.Connection, student_id: int, exam_id: int) -> bool:
    return _delete_item(conn, "exams", student_id, exam_id)

def clear_exams(conn: sqlite3.Connection, student_id: int) -> int:
    return _clear_items(conn, "exams", student_id)

def add_weekly_pattern(conn, student_id: int, item: WeeklyPattern) -> int:  # NEW
    return _add_item(conn, "weekly_patterns", student_id, item)

def get_weekly_patterns(conn, student_id: int) -> list[tuple[int, WeeklyPattern]]:  # NEW
    return _get_items(conn, "weekly_patterns", student_id, WeeklyPattern)

def delete_weekly_pattern(conn, student_id: int, item_id: int) -> bool:  # NEW
    return _delete_item(conn, "weekly_patterns", student_id, item_id)

def clear_weekly_patterns(conn, student_id: int) -> int:  # NEW
    return _clear_items(conn, "weekly_patterns", student_id)

def add_dated_block(conn, student_id: int, item: DatedBlock) -> int:  # NEW
    return _add_item(conn, "dated_blocks", student_id, item)

def get_dated_blocks(conn, student_id: int) -> list[tuple[int, DatedBlock]]:  # NEW
    return _get_items(conn, "dated_blocks", student_id, DatedBlock)

def delete_dated_block(conn, student_id: int, item_id: int) -> bool:  # NEW
    return _delete_item(conn, "dated_blocks", student_id, item_id)

def clear_dated_blocks(conn, student_id: int) -> int:  # NEW
    return _clear_items(conn, "dated_blocks", student_id)


def add_extracted_task(conn, student_id: int, item: ExtractedTask) -> int:  # NEW
    return _add_item(conn, "extracted_tasks", student_id, item)

def get_extracted_tasks(conn, student_id: int) -> list[tuple[int, ExtractedTask]]:  # NEW
    return _get_items(conn, "extracted_tasks", student_id, ExtractedTask)

def delete_extracted_task(conn, student_id: int, item_id: int) -> bool:  # NEW
    return _delete_item(conn, "extracted_tasks", student_id, item_id)

def clear_extracted_tasks(conn, student_id: int) -> int:  # NEW
    return _clear_items(conn, "extracted_tasks", student_id)

def replace_extraction(conn, student_id: int, result: ExtractionResult) -> None:  # NEW
    """Swap this student's saved extracted items for a fresh (reviewed) result, all-or-nothing."""
    if not (result.weekly_patterns or result.dated_blocks or result.tasks):  # NEW
        raise ValueError("nothing was extracted; existing data left untouched")
    groups = [("weekly_patterns", result.weekly_patterns),
              ("dated_blocks", result.dated_blocks),
              ("extracted_tasks", result.tasks)]
    try:
        for table, items in groups:
            conn.execute(f"DELETE FROM {table} WHERE student_id = ?", (student_id,))
            for item in items:
                conn.execute(
                    f"INSERT INTO {table} (student_id, data_json, created_at) VALUES (?, ?, ?)",
                    (student_id, item.model_dump_json(), _now()),
                )
        conn.commit()  # NEW -- the only commit, so it's all-or-nothing
    except Exception:
        conn.rollback()
        raise