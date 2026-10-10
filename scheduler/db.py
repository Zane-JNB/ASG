import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import NamedTuple

from pydantic import BaseModel, ValidationError

from scheduler.models import Commute, DatedBlock, ExtractedTask, ExtractionResult, ProfileSettings, WeeklyPattern
from scheduler.preference_policy import POLICY, ApprovalMode, Tier
from scheduler.units import parse_date

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

def _migrate(conn: sqlite3.Connection) -> None:
    """Additive schema changes for databases made before a column existed."""
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

def connect(path: str) -> sqlite3.Connection:
    """Open (and initialize, if new) the database at path. ':memory:' works for tests."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)  # creates any missing table
    _migrate(conn)
    return conn

def _now() -> str:
    """The current UTC time as ISO text, for created_at/updated_at columns."""
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
    """{field: tier} for every POLICY field: the student's stored choice, else the field's
    default tier. Reading never writes; only set_tier stores a row."""
    rows = dict(conn.execute("SELECT field, tier FROM preference_tiers WHERE student_id = ?",
                             (student_id,)).fetchall())
    return {name: Tier(rows[name]) if name in rows else p.default_tier for name, p in POLICY.items()}

def set_tier(conn: sqlite3.Connection, student_id: int, field: str, tier: Tier) -> None:
    """Insert or overwrite who owns one setting."""
    with transaction(conn):
        conn.execute("""INSERT INTO preference_tiers (student_id, field, tier) VALUES (?, ?, ?)
                        ON CONFLICT(student_id, field) DO UPDATE SET tier = excluded.tier""",
                     (student_id, field, tier.value))

def _utc(dt: datetime) -> datetime:
    """Naive datetimes are read as UTC, so naive and aware values can be compared."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_evidence(conn: sqlite3.Connection, student_id: int,
                  fresh_since: datetime | None = None) -> dict[str, tuple[int, str]]:
    """{field: (score, magnitude)}: the net vote of past reflections for each setting. With
    fresh_since, only evidence that changed after it (a row with no time counts as fresh)."""
    rows = conn.execute("SELECT field, score, magnitude, updated_at FROM preference_evidence "
                        "WHERE student_id = ?", (student_id,)).fetchall()
    return {f: (score, mag) for f, score, mag, at in rows
            if fresh_since is None or not at or _utc(datetime.fromisoformat(at)) > _utc(fresh_since)}

def save_evidence(conn: sqlite3.Connection, student_id: int, field: str, score: int, magnitude: str,
                  at: str | None = None) -> None:
    """Insert or overwrite one setting's evidence, stamped `at` (default: now)."""
    with transaction(conn):
        conn.execute("""INSERT INTO preference_evidence (student_id, field, score, magnitude, updated_at)
                        VALUES (?, ?, ?, ?, ?) ON CONFLICT(student_id, field) DO UPDATE SET
                        score = excluded.score, magnitude = excluded.magnitude,
                        updated_at = excluded.updated_at""",
                     (student_id, field, score, magnitude, at or _now()))

def load_approval_mode(conn: sqlite3.Connection, student_id: int) -> ApprovalMode:
    """How learned changes are approved (auto unless the student chose ask)."""
    row = conn.execute("SELECT approval_mode FROM preference_settings WHERE student_id = ?",
                       (student_id,)).fetchone()
    return ApprovalMode(row[0]) if row else ApprovalMode.AUTO

def save_approval_mode(conn: sqlite3.Connection, student_id: int, mode: ApprovalMode) -> None:
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

@dataclass(frozen=True)
class ReflectionRow:
    """One logged reflection or settings change, as get_reflections reads it back."""
    id: int
    created_at: str
    reflection_text: str
    settings_before: ProfileSettings
    settings_after: ProfileSettings
    applied: bool
    outcome: str | None  # an Outcome value; None on rows logged before outcomes existed


def get_reflections(conn: sqlite3.Connection, student_id: int) -> list[ReflectionRow]:
    """This student's reflection history, oldest first."""
    rows = conn.execute(
        """
        SELECT id, created_at, reflection_text, settings_before_json, settings_after_json, applied, outcome
        FROM reflections WHERE student_id = ? ORDER BY id ASC
        """,
        (student_id,),
    ).fetchall()
    return [ReflectionRow(row_id, created_at, text, ProfileSettings.model_validate_json(before),
                          ProfileSettings.model_validate_json(after), bool(applied), outcome)
            for row_id, created_at, text, before, after, applied, outcome in rows]


def _raw_dict(data: str) -> dict:
    """A saved row's JSON as a dict ({} if it isn't one), for rows that fail their model's checks."""
    try:
        raw = json.loads(data)
    except ValueError:
        return {}
    return raw if isinstance(raw, dict) else {}

@dataclass(frozen=True)
class ItemTable[M: BaseModel]:
    """One table of a student's saved items, each row one `model` stored as JSON.
    `label` is what the student calls a row ("class", "task", ...)."""
    name: str
    model: type[M]
    label: str

    def add(self, conn: sqlite3.Connection, student_id: int, item: M) -> int:
        """Save a new item. Returns its id."""
        with transaction(conn):
            return conn.execute(
                f"INSERT INTO {self.name} (student_id, data_json, created_at) VALUES (?, ?, ?)",
                (student_id, item.model_dump_json(), _now()),
            ).lastrowid

    def rows(self, conn: sqlite3.Connection, student_id: int) -> list[tuple[int, str]]:
        """[(id, raw JSON)], oldest first, unchecked."""
        return conn.execute(f"SELECT id, data_json FROM {self.name} WHERE student_id = ? ORDER BY id ASC",
                            (student_id,)).fetchall()

    def read(self, conn: sqlite3.Connection, student_id: int) -> tuple[list[tuple[int, M]], list["Unreadable"]]:
        """([(id, item)], [Unreadable]), oldest first. Rows saved before a check existed (e.g. an
        impossible date) are reported and left in the database untouched, never dropped or rewritten."""
        readable, unreadable = [], []
        for row_id, data in self.rows(conn, student_id):
            try:
                readable.append((row_id, self.model.model_validate_json(data)))
            except ValidationError as e:
                reason = "; ".join(err["msg"] for err in e.errors())
                unreadable.append(Unreadable(self, row_id, reason, bool(_raw_dict(data).get("completed_at"))))
        return readable, unreadable

    def get(self, conn: sqlite3.Connection, student_id: int) -> list[tuple[int, M]]:
        """[(id, item)] for every readable row, oldest first."""
        return self.read(conn, student_id)[0]

    def find(self, conn: sqlite3.Connection, student_id: int, item_id: int) -> M | None:
        """One item; None if it is missing, another student's, or unreadable."""
        return dict(self.get(conn, student_id)).get(item_id)

    def update(self, conn: sqlite3.Connection, student_id: int, item_id: int, item: M) -> bool:
        """Overwrite one item (scoped to this student). False if there was no such row."""
        with transaction(conn):
            return conn.execute(f"UPDATE {self.name} SET data_json = ? WHERE id = ? AND student_id = ?",
                                (item.model_dump_json(), item_id, student_id)).rowcount > 0

    def delete(self, conn: sqlite3.Connection, student_id: int, item_id: int) -> bool:
        """Delete one item (scoped to this student). False if there was no such row."""
        with transaction(conn):
            return conn.execute(f"DELETE FROM {self.name} WHERE id = ? AND student_id = ?",
                                (item_id, student_id)).rowcount > 0

    def clear(self, conn: sqlite3.Connection, student_id: int) -> int:
        """Delete every item of this student. Returns how many rows were removed."""
        with transaction(conn):
            return conn.execute(f"DELETE FROM {self.name} WHERE student_id = ?", (student_id,)).rowcount

class Unreadable(NamedTuple):
    """A saved row that no longer passes its model's checks: skipped by the planner, kept until
    the student deletes it."""
    table: ItemTable
    row_id: int
    reason: str
    closed: bool  # a finished task: history, never planned

WEEKLY_PATTERNS = ItemTable("weekly_patterns", WeeklyPattern, "class")
DATED_BLOCKS = ItemTable("dated_blocks", DatedBlock, "dated session")
EXTRACTED_TASKS = ItemTable("extracted_tasks", ExtractedTask, "task")
COMMUTES = ItemTable("commutes", Commute, "commute")
PLANNER_TABLES = (WEEKLY_PATTERNS, DATED_BLOCKS, EXTRACTED_TASKS, COMMUTES)  # every table the planner reads

def get_unreadable_items(conn: sqlite3.Connection, student_id: int) -> list[Unreadable]:
    """Every saved row, in any planner table, that no longer passes its model's checks."""
    return [u for table in PLANNER_TABLES for u in table.read(conn, student_id)[1]]

def has_saved_items(conn: sqlite3.Connection, student_id: int) -> bool:
    """Anything saved to plan around: a class, dated session, task or commute (readable or not)."""
    return any(table.rows(conn, student_id) for table in PLANNER_TABLES)

def skip_commute_date(conn: sqlite3.Connection, student_id: int, item_id: int, day: str) -> bool:
    """Leave one date out of a recurring commute. False if there is no such recurring commute."""
    day = parse_date(day)  # raises ValueError on a bad date
    found = COMMUTES.find(conn, student_id, item_id)
    if found is None or not found.recurring:
        return False
    if day not in found.skip_dates:  # skipping twice is harmless
        found.skip_dates.append(day)
        COMMUTES.update(conn, student_id, item_id, found)
    return True

class ImportCounts(NamedTuple):
    weekly: int  # weekly classes saved (the old ones replaced)
    dated: int  # dated sessions saved (the old ones replaced)
    tasks_added: int
    tasks_skipped: int  # repeats of a saved task

def replace_extraction(conn: sqlite3.Connection, student_id: int, result: ExtractionResult) -> ImportCounts:
    """Save a reviewed import, all-or-nothing. Fixed blocks are REPLACED, tasks are ADDED:
    weekly patterns and dated blocks are each replaced only if the import contains some
    (so an exam sheet can't wipe your classes); tasks are appended, skipping exact repeats
    (same title and due date, ignoring case; a different due time is still a repeat)."""
    if not (result.weekly_patterns or result.dated_blocks or result.tasks):
        raise ValueError("nothing was extracted; existing data left untouched")

    def repeat_key(title: object, day: object) -> tuple[str, object]:
        return str(title).strip().lower(), day

    added = skipped = 0
    with transaction(conn):
        for table, items in ((WEEKLY_PATTERNS, result.weekly_patterns), (DATED_BLOCKS, result.dated_blocks)):
            if items:  # an import with none of this type leaves the old ones alone
                table.clear(conn, student_id)
                for item in items:
                    table.add(conn, student_id, item)

        # from the raw rows, so an unreadable saved task still counts as a repeat
        saved = (_raw_dict(data) for _, data in EXTRACTED_TASKS.rows(conn, student_id))
        seen = {repeat_key(raw.get("title", ""), raw.get("date")) for raw in saved if raw}
        for task in result.tasks:  # tasks are appended, never replaced
            key = repeat_key(task.title, task.date)
            if key in seen:
                skipped += 1
                continue
            seen.add(key)
            EXTRACTED_TASKS.add(conn, student_id, task)
            added += 1
    return ImportCounts(len(result.weekly_patterns), len(result.dated_blocks), added, skipped)

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
        new_id = EXTRACTED_TASKS.add(conn, student_id, new_task)
        if new_task_cut > 0:  # saved at full hours; the shortening is plan-only, like any cut
            add_plan_cut(conn, student_id, new_id, new_task_cut)
        return new_id

def record_plan_sessions(conn: sqlite3.Connection, student_id: int, now_iso: str,
                         sessions: list[tuple[int, str, str]]) -> None:
    """Replace the not-yet-started sessions of the previous plan with the new plan's
    [(task id, start, end)]. Sessions that already started (in progress or ended) stay until the
    student has been asked about them."""
    with transaction(conn):
        conn.execute("DELETE FROM plan_sessions WHERE student_id = ? AND start_at > ?", (student_id, now_iso))
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
