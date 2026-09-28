from datetime import date

import pytest

from scheduler.models import WeeklyPattern, ExtractedTask
from scheduler.calendar_utils import (
    weekday_name, day_index_for_date, expand_weekly_pattern, expand_weekly_patterns,
    extracted_task_to_dynamic_task, extracted_tasks_to_dynamic_tasks, FixedBlock, WeeklyPattern, ExtractedTask, time_to_slot,find_overlaps
)


def test_weekday_name_matches_known_dates():
    assert weekday_name(date(2026, 9, 28)) == "Mon"  # confirmed Monday
    assert weekday_name(date(2026, 10, 3)) == "Sat"


def test_day_index_for_same_date_is_zero():
    d = date(2026, 9, 28)
    assert day_index_for_date(d, d) == 0


def test_day_index_counts_forward_correctly():
    start = date(2026, 9, 28)
    assert day_index_for_date(start, date(2026, 10, 3)) == 5


def test_day_index_rejects_date_before_start():
    start = date(2026, 9, 28)
    with pytest.raises(ValueError):
        day_index_for_date(start, date(2026, 9, 27))

def test_expand_weekly_pattern_over_two_weeks():
    start = date(2026, 9, 28)
    pattern = WeeklyPattern(title="Class", days_of_week=["Mon", "Wed", "Fri"],
                            start_time="09:00", end_time="11:00")
    blocks = expand_weekly_pattern(pattern, start, num_days=14)
    assert len(blocks) == 6  # 3 matching days x 2 weeks


def test_expand_weekly_pattern_sets_correct_times():
    start = date(2026, 9, 28)
    pattern = WeeklyPattern(title="Class", days_of_week=["Mon"],
                            start_time="09:00", end_time="11:00")
    blocks = expand_weekly_pattern(pattern, start, num_days=1)
    assert blocks[0].start_slot == 36  # 09:00
    assert blocks[0].end_slot == 44   # 11:00


def test_expand_weekly_patterns_combines_multiple():
    start = date(2026, 9, 28)
    p1 = WeeklyPattern(title="A", days_of_week=["Mon"], start_time="09:00", end_time="11:00")
    p2 = WeeklyPattern(title="B", days_of_week=["Tue"], start_time="13:00", end_time="15:00")
    blocks = expand_weekly_patterns([p1, p2], start, num_days=7)
    assert {b.title for b in blocks} == {"A", "B"}
    assert len(blocks) == 2


def test_extracted_task_to_dynamic_task_converts_date_to_day_index():
    start = date(2026, 9, 28)
    task = ExtractedTask(title="Essay", date="2026-10-05")
    dt = extracted_task_to_dynamic_task(task, start)
    assert dt.deadline_day == 7
    assert dt.title == "Essay"


def test_extracted_task_to_dynamic_task_carries_placeholder_fields():
    start = date(2026, 9, 28)
    task = ExtractedTask(title="Essay", date="2026-10-05", duration_slots=8, priority=4, difficulty=2)
    dt = extracted_task_to_dynamic_task(task, start)
    assert dt.duration_slots == 8
    assert dt.priority == 4
    assert dt.difficulty == 2


def test_extracted_task_before_plan_start_raises():
    start = date(2026, 9, 28)
    task = ExtractedTask(title="Old", date="2026-09-01")
    with pytest.raises(ValueError):
        extracted_task_to_dynamic_task(task, start)


def test_batch_conversion_skips_tasks_before_plan_start():
    start = date(2026, 9, 28)
    old = ExtractedTask(title="Old", date="2026-09-01")
    new = ExtractedTask(title="New", date="2026-10-05")
    result = extracted_tasks_to_dynamic_tasks([old, new], start)
    assert [t.title for t in result] == ["New"]

def test_batch_conversion_with_all_valid_tasks():
    start = date(2026, 9, 28)
    tasks = [ExtractedTask(title=f"T{i}", date="2026-10-05") for i in range(3)]
    result = extracted_tasks_to_dynamic_tasks(tasks, start)
    assert len(result) == 3

def test_expand_weekly_pattern_matches_correct_day():
    start = date(2026, 9, 28)  # Monday
    pattern = WeeklyPattern(title="Class", day="Wed", start_time="09:00", end_time="11:00")
    blocks = expand_weekly_pattern(pattern, start, num_days=7)
    assert [b.day for b in blocks] == [2]  # the one Wednesday in the first week

def test_expand_weekly_pattern_over_two_weeks():
    start = date(2026, 9, 28)
    pattern = WeeklyPattern(title="Class", day="Mon", start_time="09:00", end_time="11:00")
    blocks = expand_weekly_pattern(pattern, start, num_days=14)
    assert len(blocks) == 2  # the two Mondays across 2 weeks

def test_expand_weekly_pattern_sets_correct_times():
    start = date(2026, 9, 28)
    pattern = WeeklyPattern(title="Class", day="Mon", start_time="09:00", end_time="11:00")
    blocks = expand_weekly_pattern(pattern, start, num_days=1)
    assert blocks[0].start_slot == 36  # 09:00
    assert blocks[0].end_slot == 44   # 11:00

def test_expand_weekly_pattern_wrong_day_matches_nothing():
    start = date(2026, 9, 28)  # Monday
    pattern = WeeklyPattern(title="Lab", day="Sun", start_time="14:00", end_time="16:00")
    blocks = expand_weekly_pattern(pattern, start, num_days=6)  # plan ends before the first Sunday
    assert blocks == []

def test_expand_weekly_patterns_combines_multiple():
    start = date(2026, 9, 28)
    p1 = WeeklyPattern(title="A", day="Mon", start_time="09:00", end_time="11:00")
    p2 = WeeklyPattern(title="B", day="Tue", start_time="13:00", end_time="15:00")
    blocks = expand_weekly_patterns([p1, p2], start, num_days=7)
    assert {b.title for b in blocks} == {"A", "B"}
    assert len(blocks) == 2

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