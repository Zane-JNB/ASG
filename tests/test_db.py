import pytest

from scheduler.db import (
    connect, get_or_create_student, load_settings, save_settings,
    log_reflection, get_reflections,
)
from scheduler.models import ProfileSettings


@pytest.fixture
def conn():
    return connect(":memory:")


def test_new_student_gets_default_settings(conn):
    sid = get_or_create_student(conn, "Zane")
    settings = load_settings(conn, sid)
    assert settings == ProfileSettings()


def test_same_name_returns_same_student_id(conn):
    sid1 = get_or_create_student(conn, "Zane")
    sid2 = get_or_create_student(conn, "Zane")
    assert sid1 == sid2


def test_different_names_get_different_ids(conn):
    sid1 = get_or_create_student(conn, "Zane")
    sid2 = get_or_create_student(conn, "Priya")
    assert sid1 != sid2


def test_save_settings_persists_changes(conn):
    sid = get_or_create_student(conn, "Zane")
    updated = ProfileSettings(buffer_slots=4, same_day_penalty=500)
    save_settings(conn, sid, updated)
    reloaded = load_settings(conn, sid)
    assert reloaded.buffer_slots == 4
    assert reloaded.same_day_penalty == 500


def test_save_settings_overwrites_not_duplicates(conn):
    sid = get_or_create_student(conn, "Zane")
    save_settings(conn, sid, ProfileSettings(buffer_slots=2))
    save_settings(conn, sid, ProfileSettings(buffer_slots=3))
    row_count = conn.execute(
        "SELECT COUNT(*) FROM profile_settings WHERE student_id = ?", (sid,)
    ).fetchone()[0]
    assert row_count == 1
    assert load_settings(conn, sid).buffer_slots == 3


def test_load_settings_for_unknown_student_raises(conn):
    with pytest.raises(ValueError):
        load_settings(conn, 999)


def test_log_and_get_reflections(conn):
    sid = get_or_create_student(conn, "Zane")
    before = load_settings(conn, sid)
    after = ProfileSettings(buffer_slots=4)
    log_reflection(conn, sid, "felt rushed today", before=before, after=after, applied=True)

    history = get_reflections(conn, sid)
    assert len(history) == 1
    entry = history[0]
    assert entry["reflection_text"] == "felt rushed today"
    assert entry["applied"] is True
    assert entry["settings_before"].buffer_slots == 1
    assert entry["settings_after"].buffer_slots == 4


def test_reflections_ordered_oldest_first(conn):
    sid = get_or_create_student(conn, "Zane")
    s = load_settings(conn, sid)
    log_reflection(conn, sid, "first", before=s, after=s, applied=False)
    log_reflection(conn, sid, "second", before=s, after=s, applied=False)

    history = get_reflections(conn, sid)
    assert [h["reflection_text"] for h in history] == ["first", "second"]


def test_reflections_scoped_to_student(conn):
    sid1 = get_or_create_student(conn, "Zane")
    sid2 = get_or_create_student(conn, "Priya")
    s = load_settings(conn, sid1)
    log_reflection(conn, sid1, "zane's reflection", before=s, after=s, applied=False)

    assert len(get_reflections(conn, sid1)) == 1
    assert len(get_reflections(conn, sid2)) == 0


def test_connect_is_idempotent():
    # connecting twice to the same file shouldn't error or wipe existing data
    import tempfile, os
    path = tempfile.mktemp(suffix=".db")
    try:
        conn1 = connect(path)
        sid = get_or_create_student(conn1, "Zane")
        conn1.close()

        conn2 = connect(path)
        assert get_or_create_student(conn2, "Zane") == sid
        conn2.close()
    finally:
        if os.path.exists(path):
            os.remove(path)

# --- saved item tables: one ItemTable per planner table ---

from scheduler.db import (
    COMMUTES, DATED_BLOCKS, EXTRACTED_TASKS, PLANNER_TABLES, WEEKLY_PATTERNS, Unreadable,
    get_unreadable_items, has_saved_items,
)
from scheduler.models import Commute, DatedBlock, ExtractedTask, WeeklyPattern


def _class(title, start="09:00"):
    return WeeklyPattern(title=title, day="Mon", start_time=start, end_time="10:00")


def _raw_row(conn, sid, table, data_json):
    """A row saved before today's checks existed."""
    cur = conn.execute(f"INSERT INTO {table} (student_id, data_json, created_at) VALUES (?, ?, ?)",
                       (sid, data_json, "2026-01-01T00:00:00"))
    conn.commit()
    return cur.lastrowid


ONE_OF_EACH = [
    (WEEKLY_PATTERNS, _class("Lab")),
    (DATED_BLOCKS, DatedBlock(title="Exam", date="2026-10-06", start_time="09:00", end_time="11:00")),
    (EXTRACTED_TASKS, ExtractedTask(title="Essay", date="2026-10-07", due_time="17:00", duration_slots=12,
                                    priority=4, difficulty=2, splittable=True, completed_at="2026-10-05T09:00")),
    (COMMUTES, Commute(start_time="08:00", length_minutes=30, recurring=True, weekday="Wed",
                       end_date="2026-12-16", skip_dates=["2026-10-07"])),
]


def test_planner_tables_are_the_four_stores_with_the_labels_students_see():
    assert PLANNER_TABLES == (WEEKLY_PATTERNS, DATED_BLOCKS, EXTRACTED_TASKS, COMMUTES)
    assert [t.label for t in PLANNER_TABLES] == ["class", "dated session", "task", "commute"]


@pytest.mark.parametrize("table, item", ONE_OF_EACH, ids=lambda x: getattr(x, "name", ""))
def test_round_trip_preserves_all_fields(conn, table, item):
    sid = get_or_create_student(conn, "Zane")
    item_id = table.add(conn, sid, item)
    assert table.get(conn, sid) == [(item_id, item)]
    assert table.find(conn, sid, item_id) == item


def test_items_returned_oldest_first(conn):
    sid = get_or_create_student(conn, "Zane")
    WEEKLY_PATTERNS.add(conn, sid, _class("First"))
    WEEKLY_PATTERNS.add(conn, sid, _class("Second", start="08:00"))
    assert [p.title for _, p in WEEKLY_PATTERNS.get(conn, sid)] == ["First", "Second"]


def test_delete_removes_only_that_one(conn):
    sid = get_or_create_student(conn, "Zane")
    WEEKLY_PATTERNS.add(conn, sid, _class("Keep"))
    gone = WEEKLY_PATTERNS.add(conn, sid, _class("Remove"))
    assert WEEKLY_PATTERNS.delete(conn, sid, gone) is True
    assert [p.title for _, p in WEEKLY_PATTERNS.get(conn, sid)] == ["Keep"]


def test_delete_nonexistent_item_returns_false(conn):
    sid = get_or_create_student(conn, "Zane")
    assert WEEKLY_PATTERNS.delete(conn, sid, 9999) is False


def test_delete_and_update_are_scoped_to_the_right_student(conn):
    zane, priya = get_or_create_student(conn, "Zane"), get_or_create_student(conn, "Priya")
    item_id = EXTRACTED_TASKS.add(conn, zane, ExtractedTask(title="Zane's task", date="2026-10-07"))
    assert EXTRACTED_TASKS.delete(conn, priya, item_id) is False
    assert EXTRACTED_TASKS.update(conn, priya, item_id, ExtractedTask(title="Hijacked", date="2026-10-07")) is False
    assert [t.title for _, t in EXTRACTED_TASKS.get(conn, zane)] == ["Zane's task"]


def test_update_overwrites_the_row(conn):
    sid = get_or_create_student(conn, "Zane")
    item_id = EXTRACTED_TASKS.add(conn, sid, ExtractedTask(title="Draft", date="2026-10-07"))
    assert EXTRACTED_TASKS.update(conn, sid, item_id, ExtractedTask(title="Final", date="2026-10-08")) is True
    assert EXTRACTED_TASKS.get(conn, sid) == [(item_id, ExtractedTask(title="Final", date="2026-10-08"))]


def test_clear_removes_all_for_that_student_only(conn):
    zane, priya = get_or_create_student(conn, "Zane"), get_or_create_student(conn, "Priya")
    WEEKLY_PATTERNS.add(conn, zane, _class("A"))
    WEEKLY_PATTERNS.add(conn, zane, _class("B"))
    WEEKLY_PATTERNS.add(conn, priya, _class("C"))
    assert WEEKLY_PATTERNS.clear(conn, zane) == 2
    assert WEEKLY_PATTERNS.get(conn, zane) == []
    assert len(WEEKLY_PATTERNS.get(conn, priya)) == 1


def test_items_isolated_between_students(conn):
    zane, priya = get_or_create_student(conn, "Zane"), get_or_create_student(conn, "Priya")
    for table, item in ONE_OF_EACH:
        table.add(conn, zane, item)
    assert all(table.get(conn, priya) == [] for table in PLANNER_TABLES)


def test_find_is_none_for_a_missing_another_students_or_an_unreadable_row(conn):
    zane, priya = get_or_create_student(conn, "Zane"), get_or_create_student(conn, "Priya")
    theirs = EXTRACTED_TASKS.add(conn, priya, ExtractedTask(title="Priya's", date="2026-10-07"))
    bad = _raw_row(conn, zane, "extracted_tasks", '{"title": "Bad", "date": "2026-02-30"}')
    assert EXTRACTED_TASKS.find(conn, zane, 9999) is None
    assert EXTRACTED_TASKS.find(conn, zane, theirs) is None
    assert EXTRACTED_TASKS.find(conn, zane, bad) is None


def test_read_splits_readable_and_unreadable_rows_and_keeps_both(conn):
    sid = get_or_create_student(conn, "Zane")
    good = EXTRACTED_TASKS.add(conn, sid, ExtractedTask(title="Good", date="2026-10-07"))
    bad = _raw_row(conn, sid, "extracted_tasks", '{"title": "Bad", "date": "2026-02-30"}')
    done = _raw_row(conn, sid, "extracted_tasks",
                    '{"title": "Old", "date": "2026-02-30", "completed_at": "2026-03-01T10:00:00"}')
    readable, unreadable = EXTRACTED_TASKS.read(conn, sid)
    assert [i for i, _ in readable] == [good]
    assert [(u.table, u.row_id, u.closed) for u in unreadable] == [
        (EXTRACTED_TASKS, bad, False), (EXTRACTED_TASKS, done, True)]  # closed: a finished task
    assert all(isinstance(u, Unreadable) and u.reason for u in unreadable)
    assert conn.execute("SELECT COUNT(*) FROM extracted_tasks").fetchone()[0] == 3  # never dropped


def test_a_row_that_is_not_json_is_unreadable_too(conn):
    sid = get_or_create_student(conn, "Zane")
    bad = _raw_row(conn, sid, "commutes", "not json")
    assert [(u.row_id, u.closed) for u in COMMUTES.read(conn, sid)[1]] == [(bad, False)]


def test_get_unreadable_items_covers_every_planner_table_in_order(conn):
    sid = get_or_create_student(conn, "Zane")
    _raw_row(conn, sid, "commutes", '{"start_time": "08:00", "length_minutes": 30, "date": "2026-02-30"}')
    _raw_row(conn, sid, "weekly_patterns", '{"title": "Lab", "day": "Mon", "start_time": "25:00", "end_time": "26:00"}')
    assert [u.table.label for u in get_unreadable_items(conn, sid)] == ["class", "commute"]


def test_has_saved_items_ignores_commutes(conn):
    sid = get_or_create_student(conn, "Zane")
    COMMUTES.add(conn, sid, ONE_OF_EACH[3][1])
    assert has_saved_items(conn, sid) is False
    _raw_row(conn, sid, "dated_blocks", "{}")  # unreadable still counts: it is saved
    assert has_saved_items(conn, sid) is True


def test_migration_backfills_old_evidence_rows_once(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)  # a database from before updated_at existed
    old.execute("CREATE TABLE preference_evidence (student_id INTEGER NOT NULL, field TEXT NOT NULL, "
                "score INTEGER NOT NULL, magnitude TEXT NOT NULL, PRIMARY KEY (student_id, field))")
    old.execute("INSERT INTO preference_evidence VALUES (1, 'buffer_slots', 2, 'small')")
    old.commit(); old.close()
    first = connect(path).execute("SELECT updated_at FROM preference_evidence").fetchone()[0]
    assert first is not None
    second = connect(path).execute("SELECT updated_at FROM preference_evidence").fetchone()[0]
    assert second == first  # a later connect leaves it alone

def test_migration_stamps_null_evidence_times_even_when_the_column_exists(tmp_path):
    import sqlite3
    path = str(tmp_path / "half.db")
    half = sqlite3.connect(path)  # column already added, but by a version that never backfilled
    half.execute("CREATE TABLE preference_evidence (student_id INTEGER NOT NULL, field TEXT NOT NULL, "
                 "score INTEGER NOT NULL, magnitude TEXT NOT NULL, updated_at TEXT, "
                 "PRIMARY KEY (student_id, field))")
    half.execute("INSERT INTO preference_evidence VALUES (1, 'buffer_slots', 2, 'small', NULL)")
    half.commit(); half.close()
    assert connect(path).execute("SELECT updated_at FROM preference_evidence").fetchone()[0] is not None


# --- one commit rule: every write runs inside db.transaction ---

from scheduler.db import add_plan_cut, reduce_plan_cut, save_evidence, set_tier, transaction
from scheduler.preference_policy import Tier


def test_every_write_is_saved_without_an_explicit_commit(tmp_path):
    path = str(tmp_path / "asg.db")
    c1 = connect(path)
    sid = get_or_create_student(c1, "Zane")
    save_settings(c1, sid, ProfileSettings(buffer_slots=4))
    set_tier(c1, sid, "buffer_slots", Tier.USER)
    save_evidence(c1, sid, "bedtime_penalty", 2, "small")
    tid = c1.execute("INSERT INTO extracted_tasks (student_id, data_json, created_at) "
                     "VALUES (?, '{}', '')", (sid,)).lastrowid
    c1.commit()
    add_plan_cut(c1, sid, tid, 8)
    reduce_plan_cut(c1, sid, tid, 3)
    c1.close()  # no commit: anything left uncommitted is lost here

    c2 = connect(path)
    assert load_settings(c2, sid).buffer_slots == 4
    assert c2.execute("SELECT tier FROM preference_tiers WHERE student_id = ? AND field = 'buffer_slots'",
                      (sid,)).fetchone() == ("user",)
    assert c2.execute("SELECT score FROM preference_evidence WHERE student_id = ?", (sid,)).fetchone() == (2,)
    assert c2.execute("SELECT slots_cut FROM plan_cuts WHERE task_id = ?", (tid,)).fetchone() == (5,)
    c2.close()


def test_nested_transactions_commit_only_when_the_outer_one_ends(tmp_path):
    path = str(tmp_path / "asg.db")
    writer, reader = connect(path), connect(path)
    sid = get_or_create_student(writer, "Zane")
    st = load_settings(writer, sid)
    with transaction(writer):
        log_reflection(writer, sid, "inner write", before=st, after=st, applied=False)  # its own transaction, nested
        assert get_reflections(reader, sid) == []  # joined the outer one: not committed yet
    assert len(get_reflections(reader, sid)) == 1
    writer.close(); reader.close()


def test_an_inner_error_the_caller_handles_undoes_only_the_inner_block(conn):
    sid = get_or_create_student(conn, "Zane")
    st = load_settings(conn, sid)
    with transaction(conn):
        log_reflection(conn, sid, "kept", before=st, after=st, applied=False)
        try:
            with transaction(conn):
                log_reflection(conn, sid, "undone", before=st, after=st, applied=False)
                raise RuntimeError("boom")
        except RuntimeError:
            pass
    assert [r["reflection_text"] for r in get_reflections(conn, sid)] == ["kept"]
