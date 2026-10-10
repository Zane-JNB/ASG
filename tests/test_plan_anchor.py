from datetime import date

import pytest
from pydantic import ValidationError

from scheduler.calendar_utils import expand_fixed_blocks, extracted_task_to_dynamic_task
from scheduler.db import (
    connect, get_or_create_student,
    add_weekly_pattern, get_weekly_patterns, clear_weekly_patterns,
    add_dated_block, get_dated_blocks,
    add_extracted_task, get_extracted_tasks,
)
from scheduler.models import PlanAnchor, WeeklyPattern, DatedBlock, ExtractedTask
from scheduler.solver import build_schedule


def test_anchor_needs_at_least_one_day():
    with pytest.raises(ValidationError):
        PlanAnchor(start_date=date(2026, 9, 28), num_days=0)


def test_roundtrip_and_student_isolation():
    conn = connect(":memory:")
    a = get_or_create_student(conn, "A")
    b = get_or_create_student(conn, "B")
    p = WeeklyPattern(title="DS", day="Mon", start_time="09:00", end_time="11:00")
    add_weekly_pattern(conn, a, p)
    assert [x for _, x in get_weekly_patterns(conn, a)] == [p]
    assert get_weekly_patterns(conn, b) == []  # B sees nothing of A's
    assert clear_weekly_patterns(conn, a) == 1


def test_build_plan_inputs_and_solve_end_to_end():
    conn = connect(":memory:")
    sid = get_or_create_student(conn, "Z")
    add_weekly_pattern(conn, sid, WeeklyPattern(title="DS", day="Mon", start_time="09:00", end_time="11:00"))
    add_dated_block(conn, sid, DatedBlock(title="in", date="2026-09-29", start_time="10:00", end_time="12:00"))
    add_dated_block(conn, sid, DatedBlock(title="far", date="2027-01-01", start_time="10:00", end_time="12:00"))
    add_extracted_task(conn, sid, ExtractedTask(title="HW", date="2026-10-02"))

    anchor = PlanAnchor(start_date=date(2026, 9, 28), num_days=7)  # a Monday
    fixed = expand_fixed_blocks(
        [x for _, x in get_weekly_patterns(conn, sid)],
        [x for _, x in get_dated_blocks(conn, sid)],
        anchor,
    )
    tasks = [extracted_task_to_dynamic_task(x, anchor.start_date, 8) for _, x in get_extracted_tasks(conn, sid)]
    assert sorted((f.title, f.day) for f in fixed) == [("DS", 0), ("in", 1)]  # "far" dropped
    assert tasks[0].deadline_day == 4
    build_schedule(fixed, tasks, num_days=anchor.num_days, time_limit_seconds=5.0)  # must not raise