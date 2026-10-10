"""Branch fix/saved-data: unreadable saved rows are skipped (never dropped) with a hard warning,
and tasks can have an optional due time that the plan and the overdue check respect."""
from datetime import date, datetime

import pytest

from scheduler.db import (
    EXTRACTED_TASKS, connect, get_or_create_student, get_unreadable_items, WEEKLY_PATTERNS,
)
from scheduler.fit_check import build_fit_inputs
from scheduler.models import ExtractedTask
from scheduler.units import SLOTS_PER_DAY
from scheduler.planner import plan_from_saved
from scheduler.review import review_extraction
from scheduler.task_manager import prompt_new_task, run_menu
from scheduler.models import ExtractionResult

D = date(2026, 10, 5)  # a Monday
EVENING = datetime(2026, 10, 5, 20, 0)


@pytest.fixture
def conn():
    return connect(":memory:")

@pytest.fixture
def sid(conn):
    return get_or_create_student(conn, "Zane")

def _raw_row(conn, sid, table, data_json):
    """A row saved before today's checks existed (written directly, as old code could have)."""
    cur = conn.execute(f"INSERT INTO {table} (student_id, data_json, created_at) VALUES (?, ?, ?)",
                       (sid, data_json, "2026-01-01T00:00:00"))
    conn.commit()
    return cur.lastrowid

def _scripted(answers):
    it = iter(answers)
    return (lambda _prompt: next(it)), []


# ---- unreadable saved rows ----

def test_unreadable_rows_are_skipped_but_kept(conn, sid):
    EXTRACTED_TASKS.add(conn, sid, ExtractedTask(title="Good", date="2026-10-07"))
    bad_task = _raw_row(conn, sid, "extracted_tasks", '{"title": "Bad", "date": "2026-02-30"}')
    _raw_row(conn, sid, "weekly_patterns",
             '{"title": "Lab", "day": "Mon", "start_time": "25:00", "end_time": "26:00"}')

    assert [t.title for _, t in EXTRACTED_TASKS.get(conn, sid)] == ["Good"]
    assert WEEKLY_PATTERNS.get(conn, sid) == []
    bad = get_unreadable_items(conn, sid)
    assert [(u.table.label, u.row_id) for u in bad][1] == ("task", bad_task)
    assert {u.table.label for u in bad} == {"class", "task"}
    rows = conn.execute("SELECT COUNT(*) FROM extracted_tasks WHERE student_id = ?", (sid,)).fetchone()[0]
    assert rows == 2  # never dropped or rewritten

def test_plan_still_works_and_warns_hard_about_unreadable_rows(conn, sid):
    EXTRACTED_TASKS.add(conn, sid, ExtractedTask(title="Good", date="2026-10-07"))
    _raw_row(conn, sid, "extracted_tasks", '{"title": "Bad", "date": "2026-02-30"}')
    _, _, items, warnings = plan_from_saved(conn, sid, now=EVENING, time_limit_seconds=10)
    assert any(i.title.startswith("Good") for i in items)
    assert [w.severity for w in warnings if w.kind == "saved_row_unreadable"] == ["hard"]

def test_only_unreadable_rows_still_plans_and_warns(conn, sid):
    _raw_row(conn, sid, "dated_blocks",
             '{"title": "Exam", "date": "2026-13-01", "start_time": "09:00", "end_time": "10:00"}')
    _, _, _, warnings = plan_from_saved(conn, sid, now=EVENING, time_limit_seconds=10)
    assert any(w.kind == "saved_row_unreadable" for w in warnings)

def test_other_students_unreadable_rows_are_not_shown(conn, sid):
    other = get_or_create_student(conn, "Other")
    _raw_row(conn, other, "extracted_tasks", '{"title": "Bad", "date": "2026-02-30"}')
    assert get_unreadable_items(conn, sid) == []

def test_task_manager_lists_and_deletes_an_unreadable_row(conn, sid):
    _raw_row(conn, sid, "extracted_tasks", '{"title": "Bad", "date": "2026-02-30"}')
    ask, shown = _scripted(["u", "1", "u", "q"])
    run_menu(conn, sid, ask, shown.append, now=EVENING)
    assert any("Saved task (id" in line for line in shown)
    assert "Deleted." in shown and "Every saved item can be read." in shown
    assert get_unreadable_items(conn, sid) == []


# ---- optional due time ----

def test_due_time_becomes_the_solver_deadline_and_is_planned_before_it(conn, sid):
    EXTRACTED_TASKS.add(conn, sid, ExtractedTask(title="Quiz prep", date="2026-10-06", due_time="10:00",
                                                 duration_slots=4))
    fit = build_fit_inputs(conn, sid, EVENING)
    (_, task), = fit.planned
    assert (task.deadline_day, task.deadline_slot) == (1, 40)
    _, _, items, warnings = plan_from_saved(conn, sid, now=EVENING, time_limit_seconds=10)
    quiz = [i for i in items if i.title.startswith("Quiz prep")]
    assert quiz and all(i.day * SLOTS_PER_DAY + i.end_slot <= SLOTS_PER_DAY + 40 for i in quiz)
    assert not [w for w in warnings if w.kind in ("task_unscheduled", "task_overdue")]

def test_task_is_overdue_once_its_due_time_passes(conn, sid):
    EXTRACTED_TASKS.add(conn, sid, ExtractedTask(title="Quiz prep", date="2026-10-05", due_time="10:00"))
    before = build_fit_inputs(conn, sid, datetime(2026, 10, 5, 9, 0))
    assert before.planned and not before.warnings
    after = build_fit_inputs(conn, sid, datetime(2026, 10, 5, 10, 0))
    assert after.planned == []
    assert [(w.severity, w.kind) for w in after.warnings] == [("hard", "task_overdue")]
    assert "2026-10-05 10:00" in after.warnings[0].message

def test_no_due_time_still_means_end_of_day(conn, sid):
    t = ExtractedTask(title="Essay", date="2026-10-05")
    assert t.due_at() == datetime(2026, 10, 6, 0, 0) and t.due_slot() == SLOTS_PER_DAY
    EXTRACTED_TASKS.add(conn, sid, t)
    assert build_fit_inputs(conn, sid, datetime(2026, 10, 5, 23, 0)).warnings == []

@pytest.mark.parametrize("bad", ["25:00", "9.30", "24:15"])
def test_bad_due_times_are_rejected(bad):
    with pytest.raises(ValueError):
        ExtractedTask(title="x", date="2026-10-05", due_time=bad)

def test_due_time_is_normalised():
    assert ExtractedTask(title="x", date="2026-10-05", due_time="9:00").due_time == "09:00"


def test_add_task_takes_an_optional_time_with_the_date():
    ask, shown = _scripted(["Quiz prep", "2026-10-06 10:00", "", "", ""])
    t = prompt_new_task(ask, shown.append, session_cap=8, now=EVENING)
    assert (t.date, t.due_time) == ("2026-10-06", "10:00")

def test_add_task_rejects_a_time_that_already_passed():
    ask, shown = _scripted(["Quiz prep", "2026-10-05 19:00", "2026-10-05 21:00", "", "", ""])
    t = prompt_new_task(ask, shown.append, session_cap=8, now=EVENING)
    assert t.due_time == "21:00"
    assert any("already passed" in line for line in shown)


@pytest.mark.parametrize("now, too_soon, fine", [
    (datetime(2026, 10, 5, 10, 5), "2026-10-05 10:15", "2026-10-05 10:30"),   # 10:15 is the slot plans start in
    (datetime(2026, 10, 5, 10, 0), "2026-10-05 10:00", "2026-10-05 10:15"),
    (datetime(2026, 10, 5, 23, 55), "2026-10-06 00:00", "2026-10-06 00:15"),  # plans start tomorrow 00:00
])
def test_add_task_rejects_a_due_time_before_the_plan_can_start(now, too_soon, fine):  # #17
    ask, shown = _scripted(["Quiz prep", too_soon, fine, "0.25", "", ""])
    t = prompt_new_task(ask, shown.append, session_cap=8, now=now)
    assert (t.date + " " + t.due_time) == fine
    assert any("already passed" in line for line in shown)


def _review_edit(answers):
    result = ExtractionResult(tasks=[ExtractedTask(title="HW", date="2026-10-02", due_time="10:00")])
    ask, shown = _scripted(answers)
    return review_extraction(result, ask=ask, show=shown.append).tasks[0]

def test_review_edit_sets_a_due_time():
    t = _review_edit(["1", "e", "", "2026-10-03 14:30", "", "", "", ""])
    assert (t.date, t.due_time) == ("2026-10-03", "14:30")

def test_review_edit_with_a_plain_date_clears_the_time():
    t = _review_edit(["1", "e", "", "2026-10-03", "", "", "", ""])
    assert (t.date, t.due_time) == ("2026-10-03", None)

def test_review_edit_enter_keeps_date_and_time():
    t = _review_edit(["1", "e", "", "", "", "", "", ""])
    assert (t.date, t.due_time) == ("2026-10-02", "10:00")


# ---- review follow-ups ----

@pytest.mark.parametrize("typed", ["00:00", "00:10", "00:14"])
def test_a_due_time_before_0015_means_the_end_of_the_day_before(typed):
    t = ExtractedTask(title="x", date="2026-10-06", due_time=typed)
    assert (t.date, t.due_time) == ("2026-10-05", "24:00")

def test_due_time_is_rounded_down_to_its_slot():
    t = ExtractedTask(title="x", date="2026-10-05", due_time="09:10")
    assert t.due_time == "09:00" and t.due_at() == datetime(2026, 10, 5, 9, 0)

def test_add_task_says_when_the_due_time_was_rounded():
    ask, shown = _scripted(["Quiz prep", "2026-10-06 09:10", "", "", ""])
    t = prompt_new_task(ask, shown.append, session_cap=8, now=EVENING)
    assert t.due_time == "09:00" and any("Saved as due 2026-10-06 09:00" in m for m in shown)

def test_overdue_check_uses_the_same_slot_as_the_solver(conn, sid):
    EXTRACTED_TASKS.add(conn, sid, ExtractedTask(title="Quiz prep", date="2026-10-05", due_time="09:10"))
    fit = build_fit_inputs(conn, sid, datetime(2026, 10, 5, 9, 5))  # past 09:00, the slot deadline
    assert fit.planned == [] and [w.kind for w in fit.warnings] == ["task_overdue"]

def test_unreadable_commute_is_reported_not_silently_dropped(conn, sid):
    _raw_row(conn, sid, "commutes", '{"start_time": "08:00", "length_minutes": 30, "date": "2026-02-30"}')
    assert [u.table.label for u in get_unreadable_items(conn, sid)] == ["commute"]
    EXTRACTED_TASKS.add(conn, sid, ExtractedTask(title="Good", date="2026-10-07"))
    _, _, _, warnings = plan_from_saved(conn, sid, now=EVENING, time_limit_seconds=5)
    assert any(w.kind == "saved_row_unreadable" and "commute" in w.message for w in warnings)

@pytest.mark.parametrize("raw, expected", [("5pm", "17:00"), ("17:00:00", "17:00"), ("5:30 PM", "17:30")])
def test_extraction_reads_loose_due_times(raw, expected):
    from scheduler.schedule_extraction import extraction_from_dict
    result = extraction_from_dict({"tasks": [{"title": "Essay", "date": "2026-10-05", "due_time": raw}]})
    assert [(t.title, t.due_time) for t in result.tasks] == [("Essay", expected)]

def test_extraction_flags_an_unreadable_due_time_instead_of_dropping_it():
    from scheduler.schedule_extraction import extraction_from_dict
    result = extraction_from_dict({"tasks": [{"title": "Essay", "date": "2026-10-05", "due_time": "noonish"}]})
    [t] = result.tasks
    assert t.due_time is None and "CHECK due time: 'noonish'" in t.title

def test_extraction_reads_a_midnight_deadline_as_the_day_before():
    from scheduler.schedule_extraction import extraction_from_dict
    result = extraction_from_dict({"tasks": [{"title": "Essay", "date": "2026-10-06", "due_time": "00:00"}]})
    assert [(t.date, t.due_time) for t in result.tasks] == [("2026-10-05", "24:00")]

def test_reimport_with_a_different_due_time_is_still_a_repeat(conn, sid):
    from scheduler.db import replace_extraction
    EXTRACTED_TASKS.add(conn, sid, ExtractedTask(title="Quiz", date="2026-10-06"))
    replace_extraction(conn, sid, ExtractionResult(tasks=[ExtractedTask(title="Quiz", date="2026-10-06", due_time="10:00")]))
    assert len(EXTRACTED_TASKS.get(conn, sid)) == 1  # never duplicated

@pytest.mark.parametrize("raw", ["17", 17])
def test_extraction_reads_a_bare_hour(raw):
    from scheduler.schedule_extraction import extraction_from_dict
    [t] = extraction_from_dict({"tasks": [{"title": "Essay", "date": "2026-10-05", "due_time": raw}]}).tasks
    assert t.due_time == "17:00"

@pytest.mark.parametrize("raw", ["", "N/A", "none", "TBA"])
def test_extraction_treats_placeholder_due_times_as_none(raw):
    from scheduler.schedule_extraction import extraction_from_dict
    [t] = extraction_from_dict({"tasks": [{"title": "Essay", "date": "2026-10-05", "due_time": raw}]}).tasks
    assert (t.title, t.due_time) == ("Essay", None)

@pytest.mark.parametrize("title", [None, "", "  "])
def test_extraction_still_drops_a_task_without_a_title(title):
    from scheduler.schedule_extraction import extraction_from_dict
    assert extraction_from_dict({"tasks": [{"title": title, "date": "2026-10-05", "due_time": "5pm"}]}).tasks == []

def test_add_task_does_not_claim_rounding_for_zero_padding():
    ask, shown = _scripted(["Quiz prep", "2026-10-06 9:00", "", "", ""])
    t = prompt_new_task(ask, shown.append, session_cap=8, now=EVENING)
    assert t.due_time == "09:00" and not any("Saved as due" in m for m in shown)

def test_add_task_midnight_today_is_already_past():
    ask, shown = _scripted(["Quiz prep", "2026-10-05 00:00", "2026-10-06", "", "", ""])
    prompt_new_task(ask, shown.append, session_cap=8, now=EVENING)
    assert any("has already passed" in m for m in shown)

def test_tasks_due_the_same_day_are_listed_by_due_time(conn, sid):
    EXTRACTED_TASKS.add(conn, sid, ExtractedTask(title="Art essay", date="2026-10-06", due_time="17:00"))
    EXTRACTED_TASKS.add(conn, sid, ExtractedTask(title="Zoology quiz", date="2026-10-06", due_time="09:00"))
    shown = []
    answers = iter(["l", "q"])
    run_menu(conn, sid, lambda _p: next(answers), shown.append, now=EVENING)
    listed = [s for s in shown if "due 2026-10-06" in s]
    assert "Zoology" in listed[0] and "Art" in listed[1]

def test_review_edit_says_when_a_plain_date_removes_the_time():
    result = ExtractionResult(tasks=[ExtractedTask(title="HW", date="2026-10-02", due_time="10:00")])
    ask, shown = _scripted(["1", "e", "", "2026-10-03", "", "", "", ""])
    review_extraction(result, ask=ask, show=shown.append)
    assert any("Due time 10:00 removed" in m for m in shown)


# ---- review round 4 ----

def test_adding_a_task_warns_when_saved_rows_were_left_out(conn, sid):
    from scheduler.add_with_fit import add_task_with_fit
    _raw_row(conn, sid, "weekly_patterns",
             '{"title": "Lab", "day": "Mon", "start_time": "25:00", "end_time": "26:00"}')
    shown = []
    add_task_with_fit(conn, sid, ExtractedTask(title="Essay", date="2026-10-07"), EVENING,
                      ask=lambda _p: "", show=shown.append)
    assert any("saved class" in m for m in shown) and "Added." in shown

def test_unreadable_completed_task_is_listed_but_not_warned_about(conn, sid):
    EXTRACTED_TASKS.add(conn, sid, ExtractedTask(title="Good", date="2026-10-07"))
    _raw_row(conn, sid, "extracted_tasks",
             '{"title": "Old", "date": "2026-02-30", "completed_at": "2026-03-01T10:00:00"}')
    assert len(get_unreadable_items(conn, sid)) == 1  # still deletable from [u]
    _, _, _, warnings = plan_from_saved(conn, sid, now=EVENING, time_limit_seconds=5)
    assert not [w for w in warnings if w.kind == "saved_row_unreadable"]

@pytest.mark.parametrize("raw, expected", [(" 17:00", "17:00"), ("17:00 ", "17:00"), (0, None), ("0", None)])
def test_extraction_padded_and_zero_due_times(raw, expected):
    from scheduler.schedule_extraction import extraction_from_dict
    [t] = extraction_from_dict({"tasks": [{"title": "Essay", "date": "2026-10-06", "due_time": raw}]}).tasks
    assert (t.title, t.date, t.due_time) == ("Essay", "2026-10-06", expected)

def test_reimport_counts_an_unreadable_saved_task_as_a_repeat(conn, sid):
    from scheduler.db import replace_extraction
    _raw_row(conn, sid, "extracted_tasks", '{"title": "Essay", "date": "2026-10-06", "due_time": "99:00"}')
    summary = replace_extraction(conn, sid, ExtractionResult(tasks=[ExtractedTask(title="Essay", date="2026-10-06")]))
    assert summary.tasks_skipped == 1

def test_extraction_leaves_classes_without_a_due_time_key():
    from scheduler.schedule_extraction import extraction_from_dict
    raw = {"weekly_patterns": [{"title": "Lab", "day": "Mon", "start_time": "09:00", "end_time": "10:00"}]}
    assert len(extraction_from_dict(raw).weekly_patterns) == 1

def test_only_commutes_still_gives_a_plan(conn, sid):  # #28
    from scheduler.db import COMMUTES
    from scheduler.models import Commute
    COMMUTES.add(conn, sid, Commute(start_time="08:00", length_minutes=30, date="2026-10-06"))
    assert plan_from_saved(conn, sid, now=EVENING, time_limit_seconds=5).anchor.start_date == EVENING.date()


def test_building_a_plan_reads_each_saved_table_once(conn, sid, monkeypatch):
    from collections import Counter
    from scheduler.db import ItemTable
    _raw_row(conn, sid, "extracted_tasks", '{"title": "Bad", "date": "2026-02-30"}')
    EXTRACTED_TASKS.add(conn, sid, ExtractedTask(title="Good", date="2026-10-07"))
    reads, real_read = Counter(), ItemTable.read
    def counting_read(self, *args):
        reads[self.name] += 1
        return real_read(self, *args)
    monkeypatch.setattr(ItemTable, "read", counting_read)
    fit = build_fit_inputs(conn, sid, EVENING)
    assert reads == Counter(weekly_patterns=1, dated_blocks=1, extracted_tasks=1, commutes=1)
    assert [w.kind for w in fit.warnings] == ["saved_row_unreadable"]  # still warned from that one read
