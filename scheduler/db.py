import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone

from pydantic import BaseModel, ValidationError
from scheduler.preference_policy import POLICY, Tier
from scheduler.units import parse_date
from scheduler.models import ProfileSettings, FixedBlock, DynamicTask, WeeklyPattern, DatedBlock, ExtractedTask, ExtractionResult,Commute

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

CREATE TABLE IF NOT EXISTS plan_cuts (  
    task_id INTEGER PRIMARY KEY REFERENCES extracted_tasks(id) ON DELETE CASCADE,
    student_id INTEGER NOT NULL REFERENCES students(id),
    slots_cut INTEGER NOT NULL CHECK (slots_cut > 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_sessions (  
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id),
    task_id INTEGER NOT NULL REFERENCES extracted_tasks(id) ON DELETE CASCADE,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    asked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS commutes (  
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id),
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preference_tiers (   
    student_id INTEGER NOT NULL REFERENCES students(id),
    field TEXT NOT NULL,
    tier TEXT NOT NULL,
    PRIMARY KEY (student_id, field)
);

CREATE TABLE IF NOT EXISTS preference_evidence (  
    student_id INTEGER NOT NULL REFERENCES students(id),
    field TEXT NOT NULL,
    score INTEGER NOT NULL,     
    magnitude TEXT NOT NULL,  
    updated_at TEXT,  
    PRIMARY KEY (student_id, field)
);

CREATE TABLE IF NOT EXISTS preference_settings ( 
    student_id INTEGER PRIMARY KEY REFERENCES students(id),
    approval_mode TEXT NOT NULL DEFAULT 'auto'     -- 'auto' | 'ask'
);
"""

# accepts a sqlite3 connection, then stores the name of each column in the reflections table into a set called 'cols'.
# If the column "outcome" is not present in the set, it adds the column to the reflections table and commits the change.
def _migrate(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        cols = {row[1] for row in conn.execute("PRAGMA table_info(reflections)")}
        if "outcome" not in cols:
            conn.execute("ALTER TABLE reflections ADD COLUMN outcome TEXT")
        ev_cols = {row[1] for row in conn.execute("PRAGMA table_info(preference_evidence)")}
        if "updated_at" not in ev_cols:
            conn.execute("ALTER TABLE preference_evidence ADD COLUMN updated_at TEXT")
        # Any row still without a time (pre-column data, or a DB whose column was added without a
        # backfill) gets "now" once, so it can expire. A NULL time would never expire (preferences.py).
        conn.execute("UPDATE preference_evidence SET updated_at = ? WHERE updated_at IS NULL", (_now(),))

# Accepts a filepath as a string, which is then connected to sqlite and stored in the 'conn' variable.
def connect(path: str) -> sqlite3.Connection: 
    """Open (and initialize, if new) the database at path. ':memory:' works for tests."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON") # The PRAGMA keyword lets certain database settings be changed for that connection.
    conn.executescript(SCHEMA) # Simply executes the database to create tables if they don't exist.
    _migrate(conn)
    return conn

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """The one commit rule: every write runs inside this. All-or-nothing: an error undoes every
    write in the block. A nested block joins the outer one, so only the outermost block commits."""
    conn.execute("SAVEPOINT tx")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK TO tx")
        conn.execute("RELEASE tx")
        raise
    conn.execute("RELEASE tx")

# accepts a user's unique username, gets their id, if they exist and returns the id
# #if the username is not in the database, then the user is prompted to create a username and is then added to the database 
def get_or_create_student(conn: sqlite3.Connection, name: str) -> int:
    """Return this student's id, creating a row (with default settings) if they're new."""
    row = conn.execute("SELECT id FROM students WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return row[0]

    with transaction(conn):
        student_id = conn.execute(
            "INSERT INTO students (name, created_at) VALUES (?, ?)", (name, _now())
        ).lastrowid
        save_settings(conn, student_id, ProfileSettings())
    return student_id

# Uses he student_id to search the database for a valid student then returns their current setings as a json
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
    with transaction(conn):
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

def load_tiers(conn: sqlite3.Connection, student_id: int) -> dict[str, Tier]:
    """{field: tier} for every POLICY field. Missing rows are filled from each field's default tier
    first (INSERT OR IGNORE, so a student's own choice is never overwritten)."""
    with transaction(conn):
        conn.executemany(
            "INSERT OR IGNORE INTO preference_tiers (student_id, field, tier) VALUES (?, ?, ?)",
            [(student_id, name, p.default_tier.value) for name, p in POLICY.items()],
        )
    rows = conn.execute("SELECT field, tier FROM preference_tiers WHERE student_id = ?",
                        (student_id,)).fetchall()
    return {f: Tier(t) for f, t in rows if f in POLICY}

def set_tier(conn: sqlite3.Connection, student_id: int, field: str, tier: Tier) -> None:
    """Insert or overwrite who owns one setting."""
    with transaction(conn):
        conn.execute("""INSERT INTO preference_tiers (student_id, field, tier) VALUES (?, ?, ?)
                        ON CONFLICT(student_id, field) DO UPDATE SET tier = excluded.tier""",
                     (student_id, field, tier.value))

def load_evidence(conn: sqlite3.Connection, student_id: int) -> dict[str, tuple[int, str]]:
    """{field: (score, magnitude)}: the net vote of past reflections for each setting."""
    rows = conn.execute("SELECT field, score, magnitude FROM preference_evidence "
                        "WHERE student_id = ?", (student_id,)).fetchall()
    return {f: (score, mag) for f, score, mag in rows}

def save_evidence(conn: sqlite3.Connection, student_id: int, field: str, score: int, magnitude: str,
                  at: str | None = None) -> None:
    """Insert or overwrite one setting's evidence, stamped `at` (default: now)."""
    with transaction(conn):
        conn.execute("""INSERT INTO preference_evidence (student_id, field, score, magnitude, updated_at)
                        VALUES (?, ?, ?, ?, ?) ON CONFLICT(student_id, field) DO UPDATE SET
                        score = excluded.score, magnitude = excluded.magnitude,
                        updated_at = excluded.updated_at""",
                     (student_id, field, score, magnitude, at or _now()))

def load_evidence_times(conn: sqlite3.Connection, student_id: int) -> dict[str, str | None]:
    """{field: when its evidence last changed}."""
    rows = conn.execute("SELECT field, updated_at FROM preference_evidence WHERE student_id = ?",
                        (student_id,)).fetchall()
    return {f: t for f, t in rows}

def load_approval_mode(conn: sqlite3.Connection, student_id: int) -> str:
    """How learned changes are approved: 'auto' (the default) or 'ask'."""
    row = conn.execute("SELECT approval_mode FROM preference_settings WHERE student_id = ?",
                       (student_id,)).fetchone()
    return row[0] if row else "auto"

def save_approval_mode(conn: sqlite3.Connection, student_id: int, mode: str) -> None:
    """Insert or overwrite the approval mode."""
    with transaction(conn):
        conn.execute("""INSERT INTO preference_settings (student_id, approval_mode) VALUES (?, ?)
                        ON CONFLICT(student_id) DO UPDATE SET approval_mode = excluded.approval_mode""",
                     (student_id, mode))

def clear_evidence(conn: sqlite3.Connection, student_id: int, field: str | None = None) -> None:
    """Forget one setting's evidence, or every setting's when field is None."""
    with transaction(conn):
        if field is None:
            conn.execute("DELETE FROM preference_evidence WHERE student_id = ?", (student_id,))
        else:
            conn.execute("DELETE FROM preference_evidence WHERE student_id = ? AND field = ?",
                         (student_id, field))

def log_reflection(conn: sqlite3.Connection, student_id: int, reflection_text: str,
                   before: ProfileSettings, after: ProfileSettings, applied: bool,
                   outcome: str | None = None) -> int:
    """Record a reflection and the settings snapshot before/after it. Returns the new row's id."""
    with transaction(conn):
        return conn.execute(
            """
            INSERT INTO reflections
                (student_id, created_at, reflection_text, settings_before_json, settings_after_json, applied, outcome)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (student_id, _now(), reflection_text,
             before.model_dump_json(), after.model_dump_json(), int(applied), outcome)
        ).lastrowid

def get_reflections(conn: sqlite3.Connection, student_id: int) -> list[dict]:
    """Return this student's reflection history, oldest first."""
    rows = conn.execute(
        """
        SELECT id, created_at, reflection_text, settings_before_json, settings_after_json, applied, outcome
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
            "outcome": r[6]
        }
        for r in rows
    ]

def _add_item(conn: sqlite3.Connection, table: str, student_id: int, item: BaseModel) -> int:
    """Insert one item (a FixedBlock, DynamicTask, or Exam) for a student. Returns its new id."""
    with transaction(conn):
        return conn.execute(
            f"INSERT INTO {table} (student_id, data_json, created_at) VALUES (?, ?, ?)",
            (student_id, item.model_dump_json(), _now()),
        ).lastrowid

def _read_items(conn: sqlite3.Connection, table: str, student_id: int, model_cls: type, strict: bool = False):
    """(readable, unreadable) for one table, oldest first. readable: (id, model_instance);
    unreadable: (id, reason, raw json) for rows saved before a check existed (e.g. an impossible date).
    Unreadable rows are left in the database untouched -- never dropped or rewritten.
    strict: raise on the first unreadable row instead."""
    rows = conn.execute(
        f"SELECT id, data_json FROM {table} WHERE student_id = ? ORDER BY id ASC", (student_id,)
    ).fetchall()
    readable, unreadable = [], []
    for row_id, data in rows:
        try:
            readable.append((row_id, model_cls.model_validate_json(data)))
        except ValidationError as e:
            if strict:
                raise
            unreadable.append((row_id, "; ".join(err["msg"] for err in e.errors()), data))
    return readable, unreadable

def _get_items(conn: sqlite3.Connection, table: str, student_id: int, model_cls: type) -> list:
    """Every readable item of this type for a student, oldest first, as (id, model_instance)
    pairs. In tables get_unreadable_items reports, unreadable rows are skipped; anywhere else
    they still raise, so nothing is left out without a warning."""
    return _read_items(conn, table, student_id, model_cls, strict=table not in _CHECKED_TABLES)[0]

# label shown to the student -> (table, model): every table the planner reads.
UNREADABLE_CHECKED = {
    "class": ("weekly_patterns", WeeklyPattern),
    "dated session": ("dated_blocks", DatedBlock),
    "task": ("extracted_tasks", ExtractedTask),
    "commute": ("commutes", Commute),
}
_CHECKED_TABLES = {table for table, _ in UNREADABLE_CHECKED.values()}

def has_saved_items(conn: sqlite3.Connection, student_id: int) -> bool:
    """Any saved class, dated session or task (readable or not), without parsing the rows."""
    return any(conn.execute(f"SELECT 1 FROM {t} WHERE student_id = ? LIMIT 1", (student_id,)).fetchone()
               for t in ("weekly_patterns", "dated_blocks", "extracted_tasks"))

def _is_completed(data: str) -> bool:
    try:
        raw = json.loads(data)
    except ValueError:
        return False
    return isinstance(raw, dict) and bool(raw.get("completed_at"))

def get_unreadable_items(conn: sqlite3.Connection, student_id: int,
                         include_completed: bool = True) -> list[tuple[str, int, str]]:
    """(label, row id, reason) for every saved row that no longer passes its model's checks.
    include_completed=False leaves out finished tasks: they are history and never planned."""
    return [(label, row_id, reason)
            for label, (table, model) in UNREADABLE_CHECKED.items()
            for row_id, reason, data in _read_items(conn, table, student_id, model)[1]
            if include_completed or not _is_completed(data)]

def delete_unreadable_item(conn: sqlite3.Connection, student_id: int, label: str, item_id: int) -> bool:
    """Delete one unreadable row the student chose to remove (scoped to this student)."""
    return _delete_item(conn, UNREADABLE_CHECKED[label][0], student_id, item_id)

def _delete_item(conn: sqlite3.Connection, table: str, student_id: int, item_id: int) -> bool:
    """Delete one item by id, scoped to this student (so one student can't delete another's row)."""
    with transaction(conn):
        return conn.execute(
            f"DELETE FROM {table} WHERE id = ? AND student_id = ?", (item_id, student_id)
        ).rowcount > 0

def _clear_items(conn: sqlite3.Connection, table: str, student_id: int) -> int:
    """Delete every item of this type for a student. Returns how many rows were removed."""
    with transaction(conn):
        return conn.execute(f"DELETE FROM {table} WHERE student_id = ?", (student_id,)).rowcount

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

def add_weekly_pattern(conn, student_id: int, item: WeeklyPattern) -> int:   
    return _add_item(conn, "weekly_patterns", student_id, item)

def get_weekly_patterns(conn, student_id: int) -> list[tuple[int, WeeklyPattern]]:   
    return _get_items(conn, "weekly_patterns", student_id, WeeklyPattern)

def delete_weekly_pattern(conn, student_id: int, item_id: int) -> bool:   
    return _delete_item(conn, "weekly_patterns", student_id, item_id)

def clear_weekly_patterns(conn, student_id: int) -> int:   
    return _clear_items(conn, "weekly_patterns", student_id)

def add_dated_block(conn, student_id: int, item: DatedBlock) -> int:   
    return _add_item(conn, "dated_blocks", student_id, item)

def get_dated_blocks(conn, student_id: int) -> list[tuple[int, DatedBlock]]:   
    return _get_items(conn, "dated_blocks", student_id, DatedBlock)

def delete_dated_block(conn, student_id: int, item_id: int) -> bool:   
    return _delete_item(conn, "dated_blocks", student_id, item_id)

def clear_dated_blocks(conn, student_id: int) -> int:   
    return _clear_items(conn, "dated_blocks", student_id)


def add_extracted_task(conn, student_id: int, item: ExtractedTask) -> int:   
    return _add_item(conn, "extracted_tasks", student_id, item)

def get_extracted_tasks(conn, student_id: int) -> list[tuple[int, ExtractedTask]]:   
    return _get_items(conn, "extracted_tasks", student_id, ExtractedTask)

def update_extracted_task(conn, student_id: int, item_id: int, item: ExtractedTask) -> bool:   
    with transaction(conn):
        return conn.execute("UPDATE extracted_tasks SET data_json = ? WHERE id = ? AND student_id = ?",
                            (item.model_dump_json(), item_id, student_id)).rowcount > 0

def delete_extracted_task(conn, student_id: int, item_id: int) -> bool:   
    return _delete_item(conn, "extracted_tasks", student_id, item_id)

def clear_extracted_tasks(conn, student_id: int) -> int:   
    return _clear_items(conn, "extracted_tasks", student_id)

def add_commute(conn, student_id: int, item: Commute) -> int:   
    return _add_item(conn, "commutes", student_id, item)

def get_commutes(conn, student_id: int) -> list[tuple[int, Commute]]:   
    return _get_items(conn, "commutes", student_id, Commute)

def update_commute(conn, student_id: int, item_id: int, item: Commute) -> bool:   
    with transaction(conn):
        return conn.execute("UPDATE commutes SET data_json = ? WHERE id = ? AND student_id = ?",
                            (item.model_dump_json(), item_id, student_id)).rowcount > 0

def delete_commute(conn, student_id: int, item_id: int) -> bool:   
    return _delete_item(conn, "commutes", student_id, item_id)

def clear_commutes(conn, student_id: int) -> int:   
    return _clear_items(conn, "commutes", student_id)

def skip_commute_date(conn, student_id: int, item_id: int, day: str) -> bool:   
    day = parse_date(day)  # raises ValueError on a bad date
    found = {i: c for i, c in get_commutes(conn, student_id)}.get(item_id)
    if found is None or not found.recurring:
        return False
    if day not in found.skip_dates:  # skipping twice is harmless
        found.skip_dates.append(day)
        update_commute(conn, student_id, item_id, found)
    return True

def replace_extraction(conn, student_id: int, result: ExtractionResult) -> dict:   
    """Save a reviewed import, all-or-nothing. Fixed blocks are REPLACED, tasks are ADDED:
    weekly patterns and dated blocks are each replaced only if the import contains some
    (so an exam sheet can't wipe your classes); tasks are appended, skipping exact repeats
    (same title and due date, ignoring case; a different due time is still a repeat). Returns counts of what was written."""
    if not (result.weekly_patterns or result.dated_blocks or result.tasks):
        raise ValueError("nothing was extracted; existing data left untouched")

    def insert(table, item):
        conn.execute(
            f"INSERT INTO {table} (student_id, data_json, created_at) VALUES (?, ?, ?)",
            (student_id, item.model_dump_json(), _now()),
        )

    summary = {"weekly": 0, "dated": 0, "tasks_added": 0, "tasks_skipped": 0}
    with transaction(conn):  # all-or-nothing
        for table, key, items in (("weekly_patterns", "weekly", result.weekly_patterns),
                                  ("dated_blocks", "dated", result.dated_blocks)):
            if items:  #   -- an import with none of this type leaves the old ones alone
                conn.execute(f"DELETE FROM {table} WHERE student_id = ?", (student_id,))
                for item in items:
                    insert(table, item)
                summary[key] = len(items)

        seen = set()  # from the raw rows, so an unreadable saved task still counts as a repeat
        for (data,) in conn.execute("SELECT data_json FROM extracted_tasks WHERE student_id = ?", (student_id,)):
            raw = json.loads(data)
            if isinstance(raw, dict):
                seen.add((str(raw.get("title", "")).strip().lower(), raw.get("date")))
        for task in result.tasks:  #   -- tasks are appended, never replaced
            key = (task.title.strip().lower(), task.date)
            if key in seen:
                summary["tasks_skipped"] += 1
                continue
            seen.add(key)
            insert("extracted_tasks", task)
            summary["tasks_added"] += 1
    return summary

def add_plan_cut(conn: sqlite3.Connection, student_id: int, task_id: int, slots: int) -> None:
    """Cut `slots` more from this task's plan. Cuts add up and never shrink the saved task."""
    if slots < 1:
        raise ValueError("a cut must be at least one slot")
    with transaction(conn):
        if conn.execute("SELECT 1 FROM extracted_tasks WHERE id = ? AND student_id = ?",
                        (task_id, student_id)).fetchone() is None:
            raise ValueError(f"task {task_id} does not belong to this student")
        conn.execute(
            """INSERT INTO plan_cuts (task_id, student_id, slots_cut, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET slots_cut = slots_cut + excluded.slots_cut,
                                                  updated_at = excluded.updated_at""",
            (task_id, student_id, slots, _now()))

def get_plan_cuts(conn: sqlite3.Connection, student_id: int) -> dict[int, int]:
    """{task id: slots cut from its plan}."""
    rows = conn.execute("SELECT task_id, slots_cut FROM plan_cuts WHERE student_id = ?",
                        (student_id,)).fetchall()
    return {r[0]: r[1] for r in rows}

def clear_plan_cut(conn: sqlite3.Connection, student_id: int, task_id: int) -> bool:
    """Give a task its whole cut back."""
    with transaction(conn):
        return conn.execute("DELETE FROM plan_cuts WHERE task_id = ? AND student_id = ?",
                            (task_id, student_id)).rowcount > 0

def reduce_plan_cut(conn: sqlite3.Connection, student_id: int, task_id: int, slots: int) -> None:
    """Give `slots` of a task's cut back (all of it if the cut is that small)."""
    with transaction(conn):
        # delete first when the whole cut is given back: the table forbids a cut of 0
        conn.execute("DELETE FROM plan_cuts WHERE task_id = ? AND student_id = ? AND slots_cut <= ?",
                     (task_id, student_id, slots))
        conn.execute("UPDATE plan_cuts SET slots_cut = slots_cut - ?, updated_at = ? "
                     "WHERE task_id = ? AND student_id = ?", (slots, _now(), task_id, student_id))

def apply_plan_changes(conn: sqlite3.Connection, student_id: int, cuts: dict[int, int],
                       new_task: ExtractedTask | None = None, new_task_cut: int = 0) -> int | None:
    """Save a chosen drop proposal all-or-nothing: the cuts plus (optionally) the new task.
    Returns the new task's id, or None if no task was added."""
    with transaction(conn):
        for task_id, slots in cuts.items():
            add_plan_cut(conn, student_id, task_id, slots)
        if new_task is None:
            return None
        new_id = _add_item(conn, "extracted_tasks", student_id, new_task)
        if new_task_cut > 0:  # saved at full hours; the shortening is plan-only, like any cut
            add_plan_cut(conn, student_id, new_id, new_task_cut)
        return new_id

def record_plan_sessions(conn: sqlite3.Connection, student_id: int, now_iso: str,
                         sessions: list[tuple[int, str, str]]) -> None:
    """Replace the not-yet-finished sessions of the previous plan with the new plan's
    [(task id, start, end)]. Sessions that already ended stay until the student has been asked about them."""
    with transaction(conn):
        conn.execute("DELETE FROM plan_sessions WHERE student_id = ? AND end_at > ?", (student_id, now_iso))
        conn.executemany(
            "INSERT INTO plan_sessions (student_id, task_id, start_at, end_at) VALUES (?, ?, ?, ?)",
            [(student_id, t, s, e) for t, s, e in sessions])

def due_sessions(conn: sqlite3.Connection, student_id: int, now_iso: str) -> list[tuple[int, str, str]]:
    """[(task id, start, end)] of sessions that ended and have not been asked about yet."""
    return conn.execute("SELECT task_id, start_at, end_at FROM plan_sessions "
                        "WHERE student_id = ? AND asked = 0 AND end_at <= ? ORDER BY end_at",
                        (student_id, now_iso)).fetchall()

def mark_sessions_asked(conn: sqlite3.Connection, student_id: int, task_id: int, now_iso: str) -> None:
    """Mark every ended session of this task as asked about."""
    with transaction(conn):
        conn.execute("UPDATE plan_sessions SET asked = 1 WHERE student_id = ? AND task_id = ? AND end_at <= ?",
                     (student_id, task_id, now_iso))

def clear_task_sessions(conn: sqlite3.Connection, student_id: int, task_id: int) -> None:
    """Forget every session of this task (it was closed)."""
    with transaction(conn):
        conn.execute("DELETE FROM plan_sessions WHERE student_id = ? AND task_id = ?", (student_id, task_id))
