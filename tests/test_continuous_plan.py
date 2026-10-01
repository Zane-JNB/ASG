from datetime import date, datetime, timedelta

import pytest

from scheduler.db import (
    add_dated_block, add_extracted_task, add_weekly_pattern, connect, get_or_create_student,
)
from scheduler.models import DatedBlock, ExtractedTask, WeeklyPattern
from scheduler.planner import plan_from_saved

D = date(2026, 10, 5)  # a Monday
NOW = datetime(2026, 10, 5, 15, 0)  # Monday 3pm


@pytest.fixture
def conn():
    return connect(":memory:")


@pytest.fixture
def sid(conn):
    return get_or_create_student(conn, "Zane")


def _task(title, hours, priority, due):
    return ExtractedTask(title=title, date=due.isoformat(), duration_slots=int(hours * 4),
                         priority=priority, difficulty=3)


def test_default_plan_starts_today_not_tomorrow(conn, sid):
    add_extracted_task(conn, sid, _task("Report", 2, 3, D + timedelta(days=2)))
    anchor, *_ = plan_from_saved(conn, sid, now=NOW, time_limit_seconds=10)
    assert anchor.start_date == D


def test_window_length_follows_the_last_deadline(conn, sid):
    add_extracted_task(conn, sid, _task("Report", 2, 3, D + timedelta(days=4)))
    anchor, *_ = plan_from_saved(conn, sid, now=NOW, time_limit_seconds=10)
    assert anchor.num_days == 5


def test_num_days_only_sets_a_minimum(conn, sid):
    add_extracted_task(conn, sid, _task("Report", 2, 3, D + timedelta(days=1)))
    anchor, *_ = plan_from_saved(conn, sid, 6, now=NOW, time_limit_seconds=10)
    assert anchor.num_days == 6


def test_nothing_is_scheduled_before_now(conn, sid):
    add_extracted_task(conn, sid, _task("Report", 4, 3, D + timedelta(days=1)))
    _, _, items, _ = plan_from_saved(conn, sid, now=NOW, time_limit_seconds=10)
    tasks = [i for i in items if i.kind == "task"]
    assert tasks and all(i.day * 96 + i.start_slot >= 60 for i in tasks)  # 15:00 = slot 60


def test_todays_finished_classes_are_not_shown_but_later_ones_are(conn, sid):
    add_dated_block(conn, sid, DatedBlock(title="Morning class", date=D.isoformat(),
                                          start_time="08:00", end_time="10:00"))
    add_dated_block(conn, sid, DatedBlock(title="Evening class", date=D.isoformat(),
                                          start_time="17:00", end_time="19:00"))
    _, fixed, _, _ = plan_from_saved(conn, sid, now=NOW, time_limit_seconds=10)
    titles = [b.title for b in fixed]
    assert "Evening class" in titles and "Morning class" not in titles


def test_weekly_classes_repeat_across_a_long_window(conn, sid):
    add_weekly_pattern(conn, sid, WeeklyPattern(title="DS", day="Wed", start_time="09:00", end_time="11:00"))
    add_extracted_task(conn, sid, _task("Thesis", 3, 3, D + timedelta(days=16)))
    _, fixed, _, _ = plan_from_saved(conn, sid, now=NOW, time_limit_seconds=20)
    assert sorted(b.day for b in fixed if b.title == "DS") == [2, 9, 16]


def test_task_due_today_is_planned_in_the_hours_left(conn, sid):
    add_extracted_task(conn, sid, _task("Quiz prep", 2, 5, D))
    _, _, items, warnings = plan_from_saved(conn, sid, now=NOW, time_limit_seconds=10)
    assert any(i.title.startswith("Quiz prep") and i.day == 0 for i in items)
    assert not [w for w in warnings if w.kind == "task_unscheduled"]


def test_overlap_message_is_still_clear_in_continuous_mode(conn, sid):
    for title in ("A", "B"):
        add_dated_block(conn, sid, DatedBlock(title=title, date=D.isoformat(),
                                              start_time="16:00", end_time="18:00"))
    with pytest.raises(ValueError, match="overlaps"):
        plan_from_saved(conn, sid, now=NOW)


def test_explicit_start_date_still_gives_the_old_fixed_window(conn, sid):
    add_extracted_task(conn, sid, _task("Report", 2, 3, D + timedelta(days=1)))
    anchor, *_ = plan_from_saved(conn, sid, 3, start_date=D, time_limit_seconds=10)
    assert (anchor.start_date, anchor.num_days) == (D, 3)