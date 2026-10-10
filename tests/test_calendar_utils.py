from datetime import date

import pytest

from scheduler.calendar_utils import (day_index_for_date, expand_fixed_blocks, extracted_task_to_dynamic_task,
                                      find_overlaps, window_through)
from scheduler.models import DatedBlock, ExtractedTask, FixedBlock, PlanAnchor, WeeklyPattern
from scheduler.units import time_to_slot

START = date(2026, 9, 28)  # a Monday


def _window(num_days: int) -> PlanAnchor:
    return PlanAnchor(start_date=START, num_days=num_days)


def test_day_index_for_same_date_is_zero():
    assert day_index_for_date(START, START) == 0


def test_day_index_counts_forward_correctly():
    assert day_index_for_date(START, date(2026, 10, 3)) == 5


def test_day_index_rejects_date_before_start():
    with pytest.raises(ValueError):
        day_index_for_date(START, date(2026, 9, 27))


def test_weekly_pattern_matches_correct_day():
    pattern = WeeklyPattern(title="Class", day="Wed", start_time="09:00", end_time="11:00")
    assert [b.day for b in expand_fixed_blocks([pattern], [], _window(7))] == [2]  # the one Wednesday


def test_weekly_pattern_over_two_weeks():
    pattern = WeeklyPattern(title="Class", day="Mon", start_time="09:00", end_time="11:00")
    assert len(expand_fixed_blocks([pattern], [], _window(14))) == 2  # the two Mondays


def test_weekly_pattern_sets_correct_times():
    pattern = WeeklyPattern(title="Class", day="Mon", start_time="09:00", end_time="11:00")
    [block] = expand_fixed_blocks([pattern], [], _window(1))
    assert (block.start_slot, block.end_slot) == (36, 44)  # 09:00-11:00


def test_weekly_pattern_wrong_day_matches_nothing():
    pattern = WeeklyPattern(title="Lab", day="Sun", start_time="14:00", end_time="16:00")
    assert expand_fixed_blocks([pattern], [], _window(6)) == []  # plan ends before the first Sunday


def test_weekly_patterns_combine_multiple():
    p1 = WeeklyPattern(title="A", day="Mon", start_time="09:00", end_time="11:00")
    p2 = WeeklyPattern(title="B", day="Tue", start_time="13:00", end_time="15:00")
    blocks = expand_fixed_blocks([p1, p2], [], _window(7))
    assert [b.title for b in blocks] == ["A", "B"]


def test_dated_blocks_outside_the_window_are_left_out():
    dated = [DatedBlock(title=t, date=d, start_time="10:00", end_time="12:00")
             for t, d in (("past", "2026-09-27"), ("in", "2026-09-29"), ("far", "2027-01-01"))]
    assert [(b.title, b.day) for b in expand_fixed_blocks([], dated, _window(7))] == [("in", 1)]


def test_extracted_task_to_dynamic_task_converts_date_to_day_index():
    dt = extracted_task_to_dynamic_task(ExtractedTask(title="Essay", date="2026-10-05"), START, 8)
    assert dt.deadline_day == 7
    assert dt.title == "Essay"


def test_extracted_task_to_dynamic_task_carries_placeholder_fields():
    task = ExtractedTask(title="Essay", date="2026-10-05", duration_slots=8, priority=4, difficulty=2)
    dt = extracted_task_to_dynamic_task(task, START, 6)
    assert (dt.duration_slots, dt.priority, dt.difficulty, dt.max_session_slots) == (8, 4, 2, 6)


def test_extracted_task_before_plan_start_raises():
    with pytest.raises(ValueError):
        extracted_task_to_dynamic_task(ExtractedTask(title="Old", date="2026-09-01"), START, 8)


def _fb(title, day, start, end):
    return FixedBlock(title=title, day=day, start_slot=time_to_slot(start), end_slot=time_to_slot(end))


def test_find_overlaps_detects_identical_and_partial_clashes():
    a, b, c = _fb("A", 0, "12:00", "13:50"), _fb("A2", 0, "12:00", "13:50"), _fb("C", 0, "13:00", "14:00")
    pairs = find_overlaps([a, b, c])
    assert {frozenset((x.title, y.title)) for x, y in pairs} == {
        frozenset(("A", "A2")), frozenset(("A", "C")), frozenset(("A2", "C"))}


def test_find_overlaps_ignores_back_to_back_and_other_days():
    blocks = [_fb("A", 0, "10:00", "11:00"), _fb("B", 0, "11:00", "12:00"), _fb("C", 1, "10:00", "11:00")]
    assert find_overlaps(blocks) == []


def test_find_overlaps_sees_a_block_that_crosses_midnight():
    late = FixedBlock(title="Shift", day=0, start_slot=time_to_slot("22:00"), end_slot=96 + time_to_slot("02:00"))
    early = _fb("Class", 1, "01:00", "03:00")
    assert len(find_overlaps([late, early])) == 1


def test_extracted_task_to_dynamic_task_carries_the_sleep_permission():
    allowed = ExtractedTask(title="Essay", date="2026-10-06", may_cut_sleep=True)
    assert extracted_task_to_dynamic_task(allowed, START, 8).may_cut_sleep
    assert not extracted_task_to_dynamic_task(allowed.model_copy(update={"may_cut_sleep": False}),
                                              START, 8).may_cut_sleep


def test_window_through_covers_today_to_the_last_date_and_at_least_a_week():
    today = date(2026, 10, 5)
    assert window_through(today, []) == PlanAnchor(start_date=today, num_days=7)
    assert window_through(today, ["2026-10-07", None]) == PlanAnchor(start_date=today, num_days=7)
    assert window_through(today, ["2026-11-02"]) == PlanAnchor(start_date=today, num_days=29)
    assert window_through(today, ["2026-09-01"]) == PlanAnchor(start_date=today, num_days=7)  # the past is ignored


# ---- one home for overlaps: finding them and the lines a student reads ----
from scheduler.calendar_utils import overlap_lines, overlaps_between


def test_overlaps_between_pairs_one_block_from_each_list_in_time_order():
    saved = FixedBlock(title="Saved", day=0, start_slot=40, end_slot=48)
    new = [FixedBlock(title="New", day=0, start_slot=44, end_slot=52),
           FixedBlock(title="Early", day=0, start_slot=36, end_slot=41),
           FixedBlock(title="Apart", day=1, start_slot=40, end_slot=48)]
    other_saved = FixedBlock(title="Other", day=0, start_slot=50, end_slot=60)
    assert overlaps_between(new, [saved, other_saved]) == [
        (new[1], saved), (saved, new[0]), (new[0], other_saved)]  # earlier block first, earliest pair first


def test_overlap_lines_quote_both_titles_and_cap_the_list():
    a = FixedBlock(title="A", day=1, start_slot=40, end_slot=48)
    b = FixedBlock(title="B", day=1, start_slot=44, end_slot=52)
    assert overlap_lines(date(2026, 10, 4), [(a, b)]) == ["Mon 05 Oct: 'A' 10:00-12:00 overlaps 'B' 11:00-13:00"]
    lines = overlap_lines(date(2026, 10, 4), [(a, b)] * 7)
    assert len(lines) == 6 and lines[-1] == "...and 2 more overlap(s)"


def test_a_class_or_session_clashes_only_with_its_own_kind_on_the_same_day():
    mon = WeeklyPattern(title="A", day="Mon", start_time="09:00", end_time="10:00")
    assert mon.clashes(WeeklyPattern(title="B", day="Mon", start_time="09:30", end_time="11:00"))
    assert not mon.clashes(WeeklyPattern(title="B", day="Tue", start_time="09:30", end_time="11:00"))
    assert not mon.clashes(WeeklyPattern(title="B", day="Mon", start_time="10:00", end_time="11:00"))  # back to back
    dated = DatedBlock(title="C", date="2026-10-05", start_time="09:30", end_time="11:00")  # a Monday
    assert dated.clashes(DatedBlock(title="D", date="2026-10-05", start_time="10:30", end_time="12:00"))
    assert not mon.clashes(dated) and not dated.clashes(mon)
