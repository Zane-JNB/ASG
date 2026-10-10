from datetime import date, datetime

import pytest

from scheduler.db import connect, get_or_create_student, replace_extraction
from scheduler.models import (
    WeeklyPattern, DatedBlock, ExtractedTask, ExtractionResult, PlanAnchor, ScheduledItem
)
from scheduler.planner import Plan, format_plan, plan_from_saved

MONDAY = date(2026, 9, 28)
MON_MIDNIGHT = datetime(2026, 9, 28, 0, 0)  # plans start from `now`; midnight = the whole Monday

#Creates a brand new empty database with a new student, adding 1 fixed block, 1 fixed task and one dated block.
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

#has to use MONDAY instead of today's date to prevent determinism in the assertion.
def test_end_to_end_uses_real_dates_and_meets_deadline():
    conn, sid = _setup()
    anchor, fixed, items, warnings = plan_from_saved(conn, sid, 7, now=MON_MIDNIGHT, time_limit_seconds=10)
    text = "\n".join(format_plan(Plan(anchor, fixed, items, warnings)))
    assert "Mon 28 Sep 2026" in text and "09:00-11:00  [fixed]  DS" in text
    assert "Tue 29 Sep 2026" in text and "10:00-12:00  [fixed]  Lab" in text
    hw = [i for i in items if i.title.startswith("HW")]
    assert hw and all(i.day <= 4 for i in hw)  # due Fri 2 Oct = day 4

def test_no_saved_items_raises_clear_error():
    conn = connect(":memory:")
    sid = get_or_create_student(conn, "Empty")
    with pytest.raises(ValueError, match="import_schedule"):
        plan_from_saved(conn, sid, 7, now=MON_MIDNIGHT)


def test_other_students_items_are_not_used():
    conn, sid = _setup()
    other = get_or_create_student(conn, "Other")
    with pytest.raises(ValueError):
        plan_from_saved(conn, other, 7, now=MON_MIDNIGHT)


def test_weekly_pattern_repeats_each_week():
    conn, sid = _setup(tasks=[], blocks=[])
    anchor, fixed, items, warnings = plan_from_saved(conn, sid, 14, now=MON_MIDNIGHT, time_limit_seconds=10)
    text = "\n".join(format_plan(Plan(anchor, fixed, items, warnings)))
    assert "Mon 28 Sep 2026" in text and "Mon 05 Oct 2026" in text


def test_dated_block_outside_horizon_is_ignored_not_a_crash():
    far = DatedBlock(title="Far", date="2027-01-01", start_time="10:00", end_time="12:00")
    conn, sid = _setup(blocks=[far])
    anchor, fixed, items, warnings = plan_from_saved(conn, sid, 7, now=MON_MIDNIGHT, time_limit_seconds=10)
    assert all(b.title != "Far" for b in fixed)


def test_impossible_task_shows_up_as_a_warning():
    huge = ExtractedTask(title="Thesis", date="2026-09-29", duration_slots=300)  # 75h due day 1
    conn, sid = _setup(tasks=[huge], blocks=[])
    anchor, fixed, items, warnings = plan_from_saved(conn, sid, 3, now=MON_MIDNIGHT, time_limit_seconds=10)
    assert any("Thesis" in w.message for w in warnings)
    assert "Warnings:" in "\n".join(format_plan(Plan(anchor, fixed, items, warnings)))


def test_format_plan_groups_by_real_date_and_sorts_by_time():
    anchor = PlanAnchor(start_date=MONDAY, num_days=2)
    items = [ScheduledItem(title="B", start_slot=40, end_slot=44, kind="fixed", day=1),
             ScheduledItem(title="T", start_slot=20, end_slot=24, kind="task", day=0),
             ScheduledItem(title="A", start_slot=36, end_slot=40, kind="fixed", day=0)]
    assert format_plan(Plan(anchor, [], items, [])) == [
        "Mon 28 Sep 2026",
        "  05:00-06:00  [task]  T",
        "  09:00-10:00  [fixed]  A",
        "Tue 29 Sep 2026",
        "  10:00-11:00  [fixed]  B",
    ]

def test_overlapping_saved_blocks_give_a_clear_hard_warning_naming_both():
    dup = [WeeklyPattern(title="MAT2003", day="Mon", start_time="12:00", end_time="13:50"),
           WeeklyPattern(title="MAT2003 (F2F Lecture)", day="Mon", start_time="12:00", end_time="13:50")]
    conn, sid = _setup(patterns=dup, blocks=[], tasks=[])
    *_, warnings = plan_from_saved(conn, sid, 7, now=MON_MIDNIGHT, time_limit_seconds=10)
    [w] = [w for w in warnings if w.kind == "block_overlap"]
    assert w.severity == "hard"
    assert "Mon 28 Sep" in w.message and "MAT2003" in w.message and "F2F Lecture" in w.message
    assert "import_schedule" in w.message

# ---- the hours before the first wake-up must be protected ----
def test_no_tasks_are_placed_in_the_small_hours_of_day_zero():
    task = ExtractedTask(title="Essay", date="2026-10-02", duration_slots=8)
    conn, sid = _setup(patterns=[], blocks=[], tasks=[task])
    _, _, items, _ = plan_from_saved(conn, sid, 5, now=MON_MIDNIGHT, time_limit_seconds=10)
    day0 = [i for i in items if i.kind == "task" and i.day == 0]
    assert all(i.start_slot >= 28 for i in day0)  # 28 = 07:00, when the default night's sleep ends


def test_first_morning_sleep_is_shown_and_ends_at_wake_time():
    conn, sid = _setup(patterns=[], blocks=[])  # default task keeps the extraction non-empty
    anchor, fixed, items, warnings = plan_from_saved(conn, sid, 2, now=MON_MIDNIGHT, time_limit_seconds=5)
    block = next(b for b in fixed if b.title == "Sleep (night before)")
    assert (block.day, block.start_slot, block.end_slot) == (0, 0, 28)
    assert "00:00-07:00  [fixed]  Sleep (night before)" in "\n".join(format_plan(Plan(anchor, fixed, items, warnings)))


def test_an_early_class_on_day_zero_wins_over_the_assumed_sleep():
    early = DatedBlock(title="Early", date="2026-09-28", start_time="06:00", end_time="08:00")
    conn, sid = _setup(patterns=[], blocks=[early], tasks=[])
    _, fixed, _, _ = plan_from_saved(conn, sid, 2, now=MON_MIDNIGHT, time_limit_seconds=5)  # must not raise
    block = next(b for b in fixed if b.title == "Sleep (night before)")
    assert block.end_slot == 20  # stops at 05:00: the 06:00 class minus the 1h wake-up buffer
    ready = next(b for b in fixed if b.title == "Getting ready")
    assert (ready.start_slot, ready.end_slot) == (20, 23)  # + the normal 15-min buffer = 1h

def test_plan_with_bedtime_fully_blocked_warns_instead_of_crashing():
    conn, sid = _setup(patterns=[], tasks=[], blocks=[
        DatedBlock(title="Shift", date="2026-09-28", start_time="20:00", end_time="24:00"),
        DatedBlock(title="Late shift", date="2026-09-29", start_time="00:00", end_time="02:00"),
    ])
    _, _, items, warnings = plan_from_saved(conn, sid, now=MON_MIDNIGHT, time_limit_seconds=10)
    assert any(w.kind == "sleep_short" and w.severity == "hard" and w.message.startswith("Night 0")
               for w in warnings)


def test_a_plan_names_its_parts_and_its_items_hold_every_fixed_block():
    conn, sid = _setup()
    plan = plan_from_saved(conn, sid, 7, now=MON_MIDNIGHT, time_limit_seconds=10)
    assert plan.anchor.start_date == MONDAY
    shown = {(i.day, i.start_slot, i.end_slot, i.title) for i in plan.items if i.kind == "fixed"}
    assert shown == {(b.day, b.start_slot, b.end_slot, b.title) for b in plan.fixed}


def test_commutes_alone_are_enough_to_plan():  # #28
    from scheduler.db import COMMUTES
    from scheduler.models import Commute
    conn = connect(":memory:")
    sid = get_or_create_student(conn, "Commuter")
    COMMUTES.add(conn, sid, Commute(start_time="08:00", length_minutes=30, recurring=True, weekday="Mon"))
    plan = plan_from_saved(conn, sid, 1, now=MON_MIDNIGHT, time_limit_seconds=5)
    assert any(i.title == "Commute" for i in plan.items)
