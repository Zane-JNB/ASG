from datetime import date, datetime, timedelta

import pytest

from scheduler.db import (
    add_dated_block, add_extracted_task, add_weekly_pattern, connect, get_or_create_student, add_commute
)
from scheduler.models import DatedBlock, ExtractedTask, WeeklyPattern, Commute, time_to_slot
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


def test_overlapping_blocks_are_a_hard_warning_and_tasks_avoid_both(conn, sid):
    add_dated_block(conn, sid, DatedBlock(title="A", date=D.isoformat(), start_time="16:00", end_time="18:00"))
    add_dated_block(conn, sid, DatedBlock(title="B", date=D.isoformat(), start_time="17:00", end_time="19:00"))
    add_extracted_task(conn, sid, _task("Report", 2, 3, D + timedelta(days=1)))
    _, fixed, items, warnings = plan_from_saved(conn, sid, now=NOW, time_limit_seconds=10)
    [w] = [w for w in warnings if w.kind == "block_overlap"]
    assert w.severity == "hard" and "'A' 16:00-18:00 overlaps 'B' 17:00-19:00" in w.message
    assert {"A", "B"} <= {b.title for b in fixed}  # both kept
    lo, hi = time_to_slot("16:00"), time_to_slot("19:00")  # the union of A and B
    assert not any(i.kind == "task" and i.day == 0 and i.start_slot < hi and i.end_slot > lo for i in items)


def test_many_overlaps_are_summarised(conn, sid):
    for h in range(16, 23):  # 7 pairs of identical blocks, all after NOW (15:00)
        for title in ("A", "B"):
            add_dated_block(conn, sid, DatedBlock(title=f"{title}{h}", date=D.isoformat(),
                                                  start_time=f"{h}:00", end_time=f"{h}:30"))
    add_extracted_task(conn, sid, _task("Report", 1, 3, D + timedelta(days=1)))
    *_, warnings = plan_from_saved(conn, sid, now=NOW, time_limit_seconds=10)
    overlaps = [w.message for w in warnings if w.kind == "block_overlap"]
    assert len(overlaps) == 6 and overlaps[-1].startswith("...and 2 more")


def _commute_vs_class(conn, sid, class_start="17:00", class_end="19:00", commute_start="16:30"):   
    add_weekly_pattern(conn, sid, WeeklyPattern(title="Class", day="Mon",
                                                start_time=class_start, end_time=class_end))
    add_commute(conn, sid, Commute(start_time=commute_start, length_minutes=60,
                                   recurring=True, weekday="Mon"))
    add_extracted_task(conn, sid, _task("Report", 2, 3, D + timedelta(days=1)))

def test_commute_overlap_warns_instead_of_raising(conn, sid):   
    _commute_vs_class(conn, sid)  # commute 16:30-17:30 vs class 17:00-19:00, after NOW
    _, fixed, _, warnings = plan_from_saved(conn, sid, now=NOW, time_limit_seconds=10)
    assert any(w.kind == "commute_overlap" and w.severity == "soft" for w in warnings)
    assert any(b.title == "Commute" for b in fixed)

def test_commute_already_over_gives_no_warning(conn, sid):   
    _commute_vs_class(conn, sid, "07:30", "09:00", "07:00")  # both before NOW (15:00)
    _, _, _, warnings = plan_from_saved(conn, sid, now=NOW, time_limit_seconds=10)
    assert not any(w.kind == "commute_overlap" for w in warnings)