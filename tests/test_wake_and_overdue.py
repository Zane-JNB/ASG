from datetime import date, datetime, timedelta

import pytest

from scheduler.add_with_fit import add_task_with_fit
from scheduler.db import (
    add_commute, add_dated_block, add_extracted_task, connect, get_extracted_tasks,
    get_or_create_student, update_extracted_task,
)
from scheduler.fit_check import build_fit_inputs, task_date_warnings
from scheduler.models import Commute, DatedBlock, ExtractedTask, SLOTS_PER_DAY
from scheduler.planner import plan_from_saved

D = date(2026, 10, 5)  # a Monday
NOW = datetime(2026, 10, 5, 9, 30)


@pytest.fixture
def conn():
    return connect(":memory:")

@pytest.fixture
def sid(conn):
    return get_or_create_student(conn, "Zane")

def _task(title, hours, due, **kw):
    return ExtractedTask(title=title, date=due.isoformat(), duration_slots=int(hours * 4),
                         priority=3, difficulty=3, **kw)

def _block(conn, sid, title, day, start, end):
    add_dated_block(conn, sid, DatedBlock(title=title, date=day.isoformat(), start_time=start, end_time=end))

def _shifts(conn, sid):  # the review's case: work until 21:00, an early shift the next morning
    _block(conn, sid, "Work", D, "09:30", "21:00")
    _block(conn, sid, "Work2", D + timedelta(days=1), "06:00", "12:00")


def test_last_night_must_end_a_buffer_before_the_next_mornings_shift(conn, sid):
    _shifts(conn, sid)
    fit = build_fit_inputs(conn, sid, NOW, _task("Essay", 2, D))
    assert fit.anchor.num_days == 1  # the window does not grow
    assert fit.sleep_rules[-1].latest_wake == SLOTS_PER_DAY + 20  # 06:00 minus 1h = 05:00

def test_a_commute_before_the_shift_sets_the_wake_up_instead(conn, sid):
    _shifts(conn, sid)
    add_commute(conn, sid, Commute(start_time="05:30", length_minutes=30, date=(D + timedelta(days=1)).isoformat()))
    fit = build_fit_inputs(conn, sid, NOW, _task("Essay", 2, D))
    assert fit.sleep_rules[-1].latest_wake == SLOTS_PER_DAY + 18  # 04:30

def test_nothing_the_next_morning_means_no_wake_limit(conn, sid):
    _block(conn, sid, "Work", D, "09:30", "21:00")
    fit = build_fit_inputs(conn, sid, NOW, _task("Essay", 2, D))
    assert fit.sleep_rules[-1].latest_wake is None

def test_task_that_only_fits_by_sleeping_through_the_shift_is_not_just_added(conn, sid):
    _shifts(conn, sid)
    shown = []
    add_task_with_fit(conn, sid, _task("Essay", 2, D, splittable=False), NOW,
                      ask=lambda _p: "", show=shown.append)
    assert "Added." not in shown
    assert get_extracted_tasks(conn, sid) == []

def test_saved_plan_sleep_ends_before_the_next_mornings_shift(conn, sid):
    _shifts(conn, sid)
    add_extracted_task(conn, sid, _task("Reading", 1, D))
    _, _, items, _ = plan_from_saved(conn, sid, now=NOW, time_limit_seconds=10)
    (sleep,) = [i for i in items if i.kind == "sleep"]
    assert sleep.day * SLOTS_PER_DAY + sleep.end_slot <= SLOTS_PER_DAY + 20

def test_fixed_window_plan_also_respects_the_next_morning(conn, sid):
    _shifts(conn, sid)
    add_extracted_task(conn, sid, _task("Reading", 1, D))
    _, _, items, _ = plan_from_saved(conn, sid, 1, start_date=D, now=NOW, time_limit_seconds=10)
    (sleep,) = [i for i in items if i.kind == "sleep"]
    assert sleep.day * SLOTS_PER_DAY + sleep.end_slot <= SLOTS_PER_DAY + 20


def test_overdue_and_unreadable_tasks_give_hard_warnings(conn, sid):
    add_extracted_task(conn, sid, _task("Overdue essay", 2, D - timedelta(days=4)))
    bad_id = add_extracted_task(conn, sid, _task("Bad date", 1, D))
    bad = dict(get_extracted_tasks(conn, sid))[bad_id]
    update_extracted_task(conn, sid, bad_id, bad.model_copy(update={"date": "Oct 12"}))
    warnings = task_date_warnings(conn, sid, D)
    assert [(w.severity, w.kind) for w in warnings] == [("hard", "task_overdue"), ("hard", "task_bad_date")]

def test_finished_and_future_tasks_give_no_date_warnings(conn, sid):
    add_extracted_task(conn, sid, _task("Done", 2, D - timedelta(days=4), completed_at="2026-10-01T10:00"))
    add_extracted_task(conn, sid, _task("Today", 1, D))
    assert task_date_warnings(conn, sid, D) == []

def test_saved_plan_reports_an_overdue_task(conn, sid):
    add_extracted_task(conn, sid, _task("Overdue essay", 2, D - timedelta(days=4)))
    _, _, _, warnings = plan_from_saved(conn, sid, now=NOW, time_limit_seconds=10)
    assert any(w.kind == "task_overdue" and w.severity == "hard" for w in warnings)
