import pytest

from scheduler import db
from scheduler.db import (connect, get_or_create_student, replace_extraction,
                          get_weekly_patterns, get_dated_blocks, get_extracted_tasks)
from scheduler.models import WeeklyPattern, DatedBlock, ExtractedTask, ExtractionResult


def _pattern(title="DS"):
    return WeeklyPattern(title=title, day="Mon", start_time="09:00", end_time="11:00")


def _result(title="DS"):
    return ExtractionResult(
        weekly_patterns=[_pattern(title)],
        dated_blocks=[DatedBlock(title="Lab", date="2026-10-01", start_time="10:00", end_time="12:00")],
        tasks=[ExtractedTask(title="HW", date="2026-10-02")],
    )


def _titles(conn, sid):
    return ([p.title for _, p in get_weekly_patterns(conn, sid)],
            [b.title for _, b in get_dated_blocks(conn, sid)],
            [t.title for _, t in get_extracted_tasks(conn, sid)])


def test_first_save_then_replace():
    conn = connect(":memory:"); sid = get_or_create_student(conn, "Z")
    replace_extraction(conn, sid, _result("Old"))
    replace_extraction(conn, sid, _result("New"))
    assert _titles(conn, sid) == (["New"], ["Lab"], ["HW"])  # replaced, not duplicated


def test_replace_leaves_other_students_alone():
    conn = connect(":memory:")
    a, b = get_or_create_student(conn, "A"), get_or_create_student(conn, "B")
    replace_extraction(conn, a, _result("A-old"))
    replace_extraction(conn, b, _result("B-only"))
    replace_extraction(conn, a, _result("A-new"))
    assert _titles(conn, a)[0] == ["A-new"]
    assert _titles(conn, b)[0] == ["B-only"]


def test_empty_result_is_refused_and_keeps_old_data():
    conn = connect(":memory:"); sid = get_or_create_student(conn, "Z")
    replace_extraction(conn, sid, _result("Keep"))
    with pytest.raises(ValueError):
        replace_extraction(conn, sid, ExtractionResult())
    assert _titles(conn, sid) == (["Keep"], ["Lab"], ["HW"])


def test_failure_midway_rolls_back(monkeypatch):
    conn = connect(":memory:"); sid = get_or_create_student(conn, "Z")
    replace_extraction(conn, sid, _result("Keep"))

    calls = {"n": 0}
    real_now = db._now
    def flaky_now():
        calls["n"] += 1
        if calls["n"] == 2:  # dies after the first insert, mid-replacement
            raise RuntimeError("boom")
        return real_now()
    monkeypatch.setattr(db, "_now", flaky_now)

    with pytest.raises(RuntimeError):
        replace_extraction(conn, sid, _result("New"))
    monkeypatch.undo()
    assert _titles(conn, sid) == (["Keep"], ["Lab"], ["HW"])  # old data fully restored