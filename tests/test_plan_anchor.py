from datetime import date

import pytest
from pydantic import ValidationError

from scheduler.calendar_utils import expand_fixed_blocks, extracted_task_to_dynamic_task
from scheduler.db import connect, get_or_create_student, WEEKLY_PATTERNS, DATED_BLOCKS, EXTRACTED_TASKS
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
    WEEKLY_PATTERNS.add(conn, a, p)
    assert [x for _, x in WEEKLY_PATTERNS.get(conn, a)] == [p]
    assert WEEKLY_PATTERNS.get(conn, b) == []  # B sees nothing of A's
    assert WEEKLY_PATTERNS.clear(conn, a) == 1


def test_build_plan_inputs_and_solve_end_to_end():
    conn = connect(":memory:")
    sid = get_or_create_student(conn, "Z")
    WEEKLY_PATTERNS.add(conn, sid, WeeklyPattern(title="DS", day="Mon", start_time="09:00", end_time="11:00"))
    DATED_BLOCKS.add(conn, sid, DatedBlock(title="in", date="2026-09-29", start_time="10:00", end_time="12:00"))
    DATED_BLOCKS.add(conn, sid, DatedBlock(title="far", date="2027-01-01", start_time="10:00", end_time="12:00"))
    EXTRACTED_TASKS.add(conn, sid, ExtractedTask(title="HW", date="2026-10-02"))

    anchor = PlanAnchor(start_date=date(2026, 9, 28), num_days=7)  # a Monday
    fixed = expand_fixed_blocks(
        [x for _, x in WEEKLY_PATTERNS.get(conn, sid)],
        [x for _, x in DATED_BLOCKS.get(conn, sid)],
        anchor,
    )
    tasks = [extracted_task_to_dynamic_task(x, anchor.start_date, 8) for _, x in EXTRACTED_TASKS.get(conn, sid)]
    assert sorted((f.title, f.day) for f in fixed) == [("DS", 0), ("in", 1)]  # "far" dropped
    assert tasks[0].deadline_day == 4
    build_schedule(fixed, tasks, num_days=anchor.num_days, time_limit_seconds=5.0)  # must not raise