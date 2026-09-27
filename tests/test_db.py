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