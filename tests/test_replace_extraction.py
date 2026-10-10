import pytest

from scheduler import db
from scheduler.db import (
    connect, get_or_create_student, replace_extraction, WEEKLY_PATTERNS, DATED_BLOCKS, EXTRACTED_TASKS,
)
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
    return ([p.title for _, p in WEEKLY_PATTERNS.get(conn, sid)],
            [b.title for _, b in DATED_BLOCKS.get(conn, sid)],
            [t.title for _, t in EXTRACTED_TASKS.get(conn, sid)])


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

# ---- per-type behaviour: fixed blocks are replaced, tasks are added ----
def _task(title, date="2026-10-02"):
    return ExtractedTask(title=title, date=date)


def _task_titles(conn, sid):
    return sorted(t.title for _, t in EXTRACTED_TASKS.get(conn, sid))


def test_timetable_import_replaces_classes_but_keeps_tasks():
    conn = connect(":memory:"); sid = get_or_create_student(conn, "Z")
    replace_extraction(conn, sid, ExtractionResult(tasks=[_task("Essay")]))
    replace_extraction(conn, sid, ExtractionResult(weekly_patterns=[_pattern("DS")]))
    replace_extraction(conn, sid, ExtractionResult(weekly_patterns=[_pattern("DS v2")]))
    assert [p.title for _, p in WEEKLY_PATTERNS.get(conn, sid)] == ["DS v2"]
    assert _task_titles(conn, sid) == ["Essay"]


def test_tasks_only_import_keeps_classes_and_sessions():
    conn = connect(":memory:"); sid = get_or_create_student(conn, "Z")
    replace_extraction(conn, sid, _result("Keep"))
    replace_extraction(conn, sid, ExtractionResult(tasks=[_task("New task")]))
    assert _titles(conn, sid)[:2] == (["Keep"], ["Lab"])
    assert _task_titles(conn, sid) == ["HW", "New task"]  # added, not replaced


def test_dated_only_import_does_not_wipe_weekly_classes():
    conn = connect(":memory:"); sid = get_or_create_student(conn, "Z")
    replace_extraction(conn, sid, _result("Keep"))
    exam = DatedBlock(title="Exam", date="2026-11-02", start_time="09:00", end_time="11:00")
    replace_extraction(conn, sid, ExtractionResult(dated_blocks=[exam]))
    assert _titles(conn, sid)[0] == ["Keep"]
    assert _titles(conn, sid)[1] == ["Exam"]  # dated blocks WERE replaced ("Lab" is gone)


def test_duplicate_tasks_are_skipped_ignoring_case_and_reported():
    conn = connect(":memory:"); sid = get_or_create_student(conn, "Z")
    first = replace_extraction(conn, sid, ExtractionResult(tasks=[_task("Essay"), _task("Essay")]))
    assert first._asdict() == {"weekly": 0, "dated": 0, "tasks_added": 1, "tasks_skipped": 1}  # repeat inside one import
    second = replace_extraction(conn, sid, ExtractionResult(
        tasks=[_task(" ESSAY "), _task("Essay", date="2026-10-09"), _task("Lab report")]))
    assert second.tasks_added == 2 and second.tasks_skipped == 1  # same title, different date is NEW
    assert _task_titles(conn, sid) == ["Essay", "Essay", "Lab report"]


def test_summary_reports_replaced_counts():
    conn = connect(":memory:"); sid = get_or_create_student(conn, "Z")
    assert replace_extraction(conn, sid, _result())._asdict() == {
        "weekly": 1, "dated": 1, "tasks_added": 1, "tasks_skipped": 0}

def test_a_saved_task_that_is_not_json_does_not_break_an_import():
    conn = connect(":memory:"); sid = get_or_create_student(conn, "Z")
    conn.execute("INSERT INTO extracted_tasks (student_id, data_json, created_at) VALUES (?, 'not json', '')", (sid,))
    conn.commit()
    summary = replace_extraction(conn, sid, ExtractionResult(tasks=[ExtractedTask(title="HW", date="2026-10-02")]))
    assert summary.tasks_added == 1
