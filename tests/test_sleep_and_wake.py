"""Branch fix/sleep-and-wake: the wake-up buffer follows every night's sleep, and drop proposals
only count sleep the cuts themselves cost."""
from datetime import date, datetime, timedelta

import pytest

from scheduler.db import add_commute, add_dated_block, connect, get_or_create_student
from scheduler.dropping import _sleep_sacrificed
from scheduler.fit_check import build_fit_inputs
from scheduler.models import (
    Commute, DatedBlock, DynamicTask, FixedBlock, ProfileSettings, ScheduledItem, SleepRule,
)
from scheduler.units import SLOTS_PER_DAY
from scheduler.solver import build_schedule, reachable_sleep, sleep_warnings

RULE = dict(earliest_bed=88, preferred_bed=88, latest_bed=88, length_slots=32, min_slots=24)  # bed 22:00
D = date(2026, 10, 5)


@pytest.fixture
def conn():
    return connect(":memory:")

@pytest.fixture
def sid(conn):
    return get_or_create_student(conn, "Zane")

def _sleep(items):
    return next(i for i in items if i.kind == "sleep")

def _end(item):
    return item.day * SLOTS_PER_DAY + item.end_slot

def _block(conn, sid, title, day, start, end):
    add_dated_block(conn, sid, DatedBlock(title=title, date=day.isoformat(), start_time=start, end_time=end))


# ---- solver ----

def test_sleep_ends_a_wake_buffer_before_an_early_class():
    klass = FixedBlock(title="Class", day=1, start_slot=32, end_slot=40)  # 08:00
    items, _ = build_schedule([klass], [], num_days=2, sleep_rules=[SleepRule(night=0, **RULE)])
    assert _end(_sleep(items)) <= SLOTS_PER_DAY + 28  # 07:00

def test_sleep_ends_a_wake_buffer_before_an_early_commute():
    commute = FixedBlock(title="Commute", day=1, start_slot=28, end_slot=32, buffer_before=False)  # 07:00
    items, _ = build_schedule([commute], [], num_days=2, sleep_rules=[SleepRule(night=0, **RULE)])
    assert _end(_sleep(items)) <= SLOTS_PER_DAY + 24  # 06:00

def test_no_task_starts_within_the_wake_buffer():
    task = DynamicTask(title="Essay", duration_slots=4, priority=3, earliest_start_day=1, earliest_start_slot=0)
    items, _ = build_schedule([], [task], num_days=2, sleep_rules=[SleepRule(night=0, **RULE)])
    essay = next(i for i in items if i.kind == "task")
    assert essay.day * SLOTS_PER_DAY + essay.start_slot >= _end(_sleep(items)) + 4

def test_a_zero_buffer_keeps_the_old_behaviour():
    klass = FixedBlock(title="Class", day=1, start_slot=24, end_slot=32)  # 06:00
    items, _ = build_schedule([klass], [], num_days=2, sleep_rules=[SleepRule(night=0, **RULE)],
                              settings=ProfileSettings(wake_buffer_slots=0))
    assert _end(_sleep(items)) == SLOTS_PER_DAY + 24  # straight into the class

def test_a_night_with_no_sleep_has_no_wake_buffer():
    # an all-night exam leaves no room for sleep; a task straight after it still fits
    exam = [FixedBlock(title="Exam", day=0, start_slot=88, end_slot=96),
            FixedBlock(title="Exam", day=1, start_slot=0, end_slot=40)]
    task = DynamicTask(title="Essay", duration_slots=4, priority=3, deadline_day=1, deadline_slot=45)
    items, unscheduled = build_schedule(exam, [task], num_days=2, sleep_rules=[SleepRule(night=0, **RULE)])
    assert not unscheduled and not [i for i in items if i.kind == "sleep"]


# ---- sleep warnings and drop proposals ----

def test_reachable_sleep_is_capped_by_the_morning_after():
    capped = SleepRule(night=0, latest_wake=SLOTS_PER_DAY + 20, **RULE)  # up by 05:00
    assert reachable_sleep(capped) == 28  # 22:00 -> 05:00
    assert reachable_sleep(SleepRule(night=0, **RULE)) == 32
    assert reachable_sleep(SleepRule(night=0, skip=True, **RULE)) == 0

def test_drop_proposals_do_not_blame_the_cuts_for_an_early_start():
    capped = SleepRule(night=0, latest_wake=SLOTS_PER_DAY + 20, **RULE)
    slept = [ScheduledItem(title="Sleep", day=0, start_slot=88, end_slot=116, kind="sleep")]  # all 28 allowed
    assert _sleep_sacrificed([capped], slept) == 0  # "target kept"

def test_a_short_night_names_the_next_mornings_start():
    capped = SleepRule(night=0, latest_wake=SLOTS_PER_DAY + 20, latest_wake_reason="'Work' at 06:00 the next morning", **RULE)
    slept = [ScheduledItem(title="Sleep", day=0, start_slot=88, end_slot=116, kind="sleep")]
    (warning,) = sleep_warnings([capped], slept)
    assert "It has to end by then: 'Work' at 06:00 the next morning." in warning.message

def test_the_cap_is_only_blamed_when_it_was_what_limited_the_sleep():
    capped = SleepRule(night=0, latest_wake=SLOTS_PER_DAY + 20, latest_wake_reason="'Work' at 06:00 the next morning", **RULE)
    short = [ScheduledItem(title="Sleep", day=0, start_slot=88, end_slot=98, kind="sleep")]  # something else cut it
    (warning,) = sleep_warnings([capped], short)
    assert "'Work'" not in warning.message


# ---- fit check: sleep setup ----

def test_every_night_is_capped_by_the_next_days_first_class(conn, sid):
    for k in (1, 2):
        _block(conn, sid, "Lecture", D + timedelta(days=k), "06:00", "08:00")
    fit = build_fit_inputs(conn, sid, datetime(2026, 10, 5, 9, 30), min_days=2)
    assert [r.latest_wake for r in fit.sleep_rules] == [SLOTS_PER_DAY + 20, SLOTS_PER_DAY + 20]

def test_the_reason_names_the_class_and_the_buffer(conn, sid):
    _block(conn, sid, "Work", D + timedelta(days=1), "06:00", "12:00")
    fit = build_fit_inputs(conn, sid, datetime(2026, 10, 5, 9, 30))
    assert fit.sleep_rules[-1].latest_wake == SLOTS_PER_DAY + 20
    assert fit.sleep_rules[-1].latest_wake_reason == "'Work' at 06:00 the next day (minus your 60 min wake-up buffer)"

def test_a_zero_buffer_is_not_mentioned_in_the_reason(conn, sid):
    from scheduler.preferences import Actor, set_values
    set_values(conn, sid, {"wake_buffer_slots": 0}, Actor.USER)
    _block(conn, sid, "Work", D + timedelta(days=1), "06:00", "12:00")
    fit = build_fit_inputs(conn, sid, datetime(2026, 10, 5, 9, 30))
    assert fit.sleep_rules[-1].latest_wake_reason == "'Work' at 06:00 the next day"

def test_a_commute_the_next_morning_caps_the_night_too(conn, sid):
    add_commute(conn, sid, Commute(start_time="07:00", length_minutes=30, date=(D + timedelta(days=1)).isoformat()))
    fit = build_fit_inputs(conn, sid, datetime(2026, 10, 5, 9, 30))
    assert fit.sleep_rules[-1].latest_wake == SLOTS_PER_DAY + 24  # 06:00

def test_this_morning_gets_a_getting_ready_block_before_the_first_class(conn, sid):
    _block(conn, sid, "Lab", D, "08:00", "10:00")
    fit = build_fit_inputs(conn, sid, datetime(2026, 10, 5, 0, 0))
    titles = {b.title: (b.start_slot, b.end_slot) for b in fit.fixed if b.day == 0}
    assert titles["Sleep (night before)"] == (0, 28)  # 07:00 = 08:00 minus 1h
    assert titles["Getting ready"] == (28, 31)

def test_this_morning_keeps_the_rest_of_the_wake_buffer_when_planning_inside_it(conn, sid):
    fit = build_fit_inputs(conn, sid, datetime(2026, 10, 5, 7, 10))  # woke at 07:00 (no classes)
    ready = [b for b in fit.fixed if b.title == "Getting ready"]
    assert [(b.start_slot, b.end_slot) for b in ready] == [(29, 31)]  # 07:15 -> 07:45, then the 15-min buffer

def test_getting_ready_never_overlaps_a_class_just_after_midnight(conn, sid):
    _block(conn, sid, "Exam", D, "00:30", "03:00")
    fit = build_fit_inputs(conn, sid, datetime(2026, 10, 5, 0, 0))
    ready = [b for b in fit.fixed if b.title == "Getting ready"]
    assert all(b.end_slot <= 2 for b in ready)  # never past the 00:30 exam


def test_a_late_bedtime_is_not_blamed_on_the_next_morning():
    # bed moved to 01:00 (planning late); sleep ends at 08:00, well before the 10:00 cap
    late = SleepRule(night=0, earliest_bed=100, preferred_bed=100, latest_bed=100, length_slots=32, min_slots=24,
                     latest_wake=SLOTS_PER_DAY + 40, latest_wake_reason="'Class' at 11:00 the next morning")
    slept = [ScheduledItem(title="Sleep", day=1, start_slot=4, end_slot=32, kind="sleep")]
    (warning,) = sleep_warnings([late], slept)
    assert "'Class'" not in warning.message


def test_a_night_with_no_room_at_all_still_names_the_cause():
    # a 01:00 class the next day: the cap (00:00) is before the earliest bedtime allows any sleep
    capped = SleepRule(night=0, latest_wake=88, latest_wake_reason="'Lab' at 01:00 the next day", **RULE)
    (warning,) = sleep_warnings([capped], [])  # no sleep item at all
    assert warning.severity == "hard" and "'Lab' at 01:00 the next day" in warning.message
