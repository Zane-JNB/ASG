from datetime import date, timedelta

import pytest

from scheduler.db import connect, get_or_create_student, replace_extraction
from scheduler.models import (
    WeeklyPattern, DatedBlock, ExtractedTask, ExtractionResult, FixedBlock, PlanAnchor, ScheduledItem
)
from scheduler.planner import plan_from_saved, format_plan

MONDAY = date(2026, 9, 28)


def _setup(tasks=None, blocks=None, patterns=None):
    conn = connect(":memory:")
    sid = get_or_create_student(conn, "Z")
    replace_extraction(conn, sid, ExtractionResult(
        weekly_patterns=patterns if patterns is not None else
            [WeeklyPattern(title="DS", day="Mon", start_time="09:00", end_time="11:00")],
        dated_blocks=blocks if blocks is not None else
            [DatedBlock(title="Lab", date="2026-09-29", start_time="10:00", end_time="12:00")],
        tasks=tasks if tasks is not None else
            [ExtractedTask(title="HW", date="2026-10-02", duration_slots=8)],
    ))
    return conn, sid


def test_end_to_end_uses_real_dates_and_meets_deadline():
    conn, sid = _setup()
    anchor, fixed, items, warnings = plan_from_saved(conn, sid, 7, start_date=MONDAY, time_limit_seconds=10)
    text = "\n".join(format_plan(anchor, fixed, items, warnings))
    assert "Mon 28 Sep 2026" in text and "09:00-11:00  [fixed]  DS" in text
    assert "Tue 29 Sep 2026" in text and "10:00-12:00  [fixed]  Lab" in text
    hw = [i for i in items if i.title.startswith("HW")]
    assert hw and all(i.day <= 4 for i in hw)  # due Fri 2 Oct = day 4


def test_start_date_defaults_to_tomorrow():
    conn, sid = _setup()
    anchor, *_ = plan_from_saved(conn, sid, 2, time_limit_seconds=5)
    assert anchor.start_date == date.today() + timedelta(days=1)


def test_no_saved_items_raises_clear_error():
    conn = connect(":memory:")
    sid = get_or_create_student(conn, "Empty")
    with pytest.raises(ValueError, match="import_schedule"):
        plan_from_saved(conn, sid, 7, start_date=MONDAY)


def test_other_students_items_are_not_used():
    conn, sid = _setup()
    other = get_or_create_student(conn, "Other")
    with pytest.raises(ValueError):
        plan_from_saved(conn, other, 7, start_date=MONDAY)


def test_weekly_pattern_repeats_each_week():
    conn, sid = _setup(tasks=[], blocks=[])
    anchor, fixed, items, warnings = plan_from_saved(conn, sid, 14, start_date=MONDAY, time_limit_seconds=10)
    text = "\n".join(format_plan(anchor, fixed, items, warnings))
    assert "Mon 28 Sep 2026" in text and "Mon 05 Oct 2026" in text


def test_dated_block_outside_horizon_is_ignored_not_a_crash():
    far = DatedBlock(title="Far", date="2027-01-01", start_time="10:00", end_time="12:00")
    conn, sid = _setup(blocks=[far])
    anchor, fixed, items, warnings = plan_from_saved(conn, sid, 7, start_date=MONDAY, time_limit_seconds=10)
    assert all(b.title != "Far" for b in fixed)


def test_impossible_task_shows_up_as_a_warning():
    huge = ExtractedTask(title="Thesis", date="2026-09-29", duration_slots=300)  # 75h due day 1
    conn, sid = _setup(tasks=[huge], blocks=[])
    anchor, fixed, items, warnings = plan_from_saved(conn, sid, 3, start_date=MONDAY, time_limit_seconds=10)
    assert any("Thesis" in w.message for w in warnings)
    assert "Warnings:" in "\n".join(format_plan(anchor, fixed, items, warnings))


def test_format_plan_groups_by_real_date_sorts_by_time_and_skips_duplicate_fixed():
    anchor = PlanAnchor(start_date=MONDAY, num_days=2)
    fixed = [FixedBlock(title="B", start_slot=40, end_slot=44, day=1),
             FixedBlock(title="A", start_slot=36, end_slot=40, day=0)]
    items = [ScheduledItem(title="T", start_slot=20, end_slot=24, kind="task", day=0),
             ScheduledItem(title="A", start_slot=36, end_slot=40, kind="fixed", day=0)]  # must not repeat A
    assert format_plan(anchor, fixed, items, []) == [
        "Mon 28 Sep 2026",
        "  05:00-06:00  [task]  T",
        "  09:00-10:00  [fixed]  A",
        "Tue 29 Sep 2026",
        "  10:00-11:00  [fixed]  B",
    ]

def test_overlapping_saved_blocks_give_a_clear_error_naming_both():
    dup = [WeeklyPattern(title="MAT2003", day="Mon", start_time="12:00", end_time="13:50"),
           WeeklyPattern(title="MAT2003 (F2F Lecture)", day="Mon", start_time="12:00", end_time="13:50")]
    conn, sid = _setup(patterns=dup, blocks=[], tasks=[])
    with pytest.raises(ValueError) as e:
        plan_from_saved(conn, sid, 7, start_date=MONDAY)
    msg = str(e.value)
    assert "Mon 28 Sep" in msg and "MAT2003" in msg and "F2F Lecture" in msg and "import_schedule" in msg

# ---- the hours before the first wake-up must be protected ----
def test_no_tasks_are_placed_in_the_small_hours_of_day_zero():
    task = ExtractedTask(title="Essay", date="2026-10-02", duration_slots=8)
    conn, sid = _setup(patterns=[], blocks=[], tasks=[task])
    _, _, items, _ = plan_from_saved(conn, sid, 5, start_date=MONDAY, time_limit_seconds=10)
    day0 = [i for i in items if i.kind == "task" and i.day == 0]
    assert all(i.start_slot >= 28 for i in day0)  # 28 = 07:00, when the default night's sleep ends


def test_first_morning_sleep_is_shown_and_ends_at_wake_time():
    conn, sid = _setup(patterns=[], blocks=[])  # default task keeps the extraction non-empty
    anchor, fixed, items, warnings = plan_from_saved(conn, sid, 2, start_date=MONDAY, time_limit_seconds=5)
    block = next(b for b in fixed if b.title == "Sleep (night before)")
    assert (block.day, block.start_slot, block.end_slot) == (0, 0, 28)
    assert "00:00-07:00  [fixed]  Sleep (night before)" in "\n".join(format_plan(anchor, fixed, items, warnings))


def test_an_early_class_on_day_zero_wins_over_the_assumed_sleep():
    early = DatedBlock(title="Early", date="2026-09-28", start_time="06:00", end_time="08:00")
    conn, sid = _setup(patterns=[], blocks=[early], tasks=[])
    _, fixed, _, _ = plan_from_saved(conn, sid, 2, start_date=MONDAY, time_limit_seconds=5)  # must not raise
    block = next(b for b in fixed if b.title == "Sleep (night before)")
    assert block.end_slot == 24  # stops at 06:00 where the class starts