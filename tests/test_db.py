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

# --- schedule input persistence: fixed blocks, tasks, exams ---  

from scheduler.db import (  
    add_fixed_block, get_fixed_blocks, delete_fixed_block, clear_fixed_blocks,  
    add_task, get_tasks, delete_task, clear_tasks,  
)  
from scheduler.models import FixedBlock, DynamicTask


def test_add_and_get_fixed_blocks(conn):  
    sid = get_or_create_student(conn, "Zane")   
    add_fixed_block(conn, sid, FixedBlock(title="Class A", start_slot=32, end_slot=48))  
    add_fixed_block(conn, sid, FixedBlock(title="Class B", start_slot=60, end_slot=72))  

    blocks = get_fixed_blocks(conn, sid)   
    assert len(blocks) == 2   
    assert [b.title for _, b in blocks] == ["Class A", "Class B"]   


def test_add_and_get_tasks(conn):  
    sid = get_or_create_student(conn, "Zane") 
    add_task(conn, sid, DynamicTask(title="Essay", duration_slots=8, priority=3, difficulty=2))   

    tasks = get_tasks(conn, sid)  
    assert len(tasks) == 1  
    assert tasks[0][1].title == "Essay"   
    assert tasks[0][1].duration_slots == 8  


def test_items_returned_oldest_first(conn):   
    sid = get_or_create_student(conn, "Zane")  
    add_fixed_block(conn, sid, FixedBlock(title="First", start_slot=0, end_slot=10))  
    add_fixed_block(conn, sid, FixedBlock(title="Second", start_slot=20, end_slot=30))  

    blocks = get_fixed_blocks(conn, sid)   
    assert [b.title for _, b in blocks] == ["First", "Second"]  


def test_delete_fixed_block_removes_only_that_one(conn):   
    sid = get_or_create_student(conn, "Zane")  
    id1 = add_fixed_block(conn, sid, FixedBlock(title="Keep", start_slot=0, end_slot=10))  
    id2 = add_fixed_block(conn, sid, FixedBlock(title="Remove", start_slot=20, end_slot=30)) 

    assert delete_fixed_block(conn, sid, id2) is True  
    remaining = get_fixed_blocks(conn, sid)  
    assert [b.title for _, b in remaining] == ["Keep"]   


def test_delete_nonexistent_item_returns_false(conn):  
    sid = get_or_create_student(conn, "Zane")  
    assert delete_fixed_block(conn, sid, 9999) is False   


def test_delete_is_scoped_to_the_right_student(conn): 
    sid1 = get_or_create_student(conn, "Zane")  
    sid2 = get_or_create_student(conn, "Priya")  
    block_id = add_fixed_block(conn, sid1, FixedBlock(title="Zane's class", start_slot=0, end_slot=10))   

    assert delete_fixed_block(conn, sid2, block_id) is False  
    assert len(get_fixed_blocks(conn, sid1)) == 1  


def test_clear_fixed_blocks_removes_all_for_that_student(conn):   
    sid = get_or_create_student(conn, "Zane")   
    add_fixed_block(conn, sid, FixedBlock(title="A", start_slot=0, end_slot=10))   
    add_fixed_block(conn, sid, FixedBlock(title="B", start_slot=20, end_slot=30)) 

    removed = clear_fixed_blocks(conn, sid)  
    assert removed == 2  
    assert get_fixed_blocks(conn, sid) == []   


def test_items_isolated_between_students(conn):  
    sid1 = get_or_create_student(conn, "Zane")  
    sid2 = get_or_create_student(conn, "Priya")  
    add_fixed_block(conn, sid1, FixedBlock(title="Zane's class", start_slot=0, end_slot=10))  
    add_task(conn, sid1, DynamicTask(title="Zane's task", duration_slots=4, priority=1, difficulty=1))  

    assert get_fixed_blocks(conn, sid2) == []  
    assert get_tasks(conn, sid2) == []  


def test_tasks_and_exams_support_delete_and_clear_too(conn):  
    sid = get_or_create_student(conn, "Zane")  
    tid = add_task(conn, sid, DynamicTask(title="A", duration_slots=4, priority=1, difficulty=1))  
    add_task(conn, sid, DynamicTask(title="B", duration_slots=4, priority=1, difficulty=1))  

    assert delete_task(conn, sid, tid) is True  
    assert len(get_tasks(conn, sid)) == 1  

    assert clear_tasks(conn, sid) == 1  
    assert get_tasks(conn, sid) == []  


def test_round_trip_preserves_all_fields(conn):  
    sid = get_or_create_student(conn, "Zane")  
    block = FixedBlock(title="Complex", start_slot=90, end_slot=100, day=2)  
    add_fixed_block(conn, sid, block)  
    _, reloaded_block = get_fixed_blocks(conn, sid)[0]  
    assert reloaded_block == block  

    task = DynamicTask(title="Complex task", duration_slots=20, priority=5, difficulty=4,  
                       splittable=True, max_session_slots=8, deadline_day=3, deadline_slot=50,  
                       earliest_start_day=1, earliest_start_slot=10, max_daily_slots=16)  
    add_task(conn, sid, task)  
    _, reloaded_task = get_tasks(conn, sid)[0]  
    assert reloaded_task == task  


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
