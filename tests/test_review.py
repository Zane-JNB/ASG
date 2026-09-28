import pytest
from pydantic import ValidationError

from scheduler.models import ExtractedTask, ExtractionResult, WeeklyPattern, DatedBlock
from scheduler.review import hours_to_slots, edit_extracted_task, review_extraction


def _task():
    return ExtractedTask(title="HW", date="2026-10-02")  # placeholders: 4 slots, p3, d3


@pytest.mark.parametrize("hours, slots", [(1, 4), (1.5, 6), (0.25, 1), (0.375, 2), (2.1, 8)])
def test_hours_to_slots(hours, slots):
    assert hours_to_slots(hours) == slots


@pytest.mark.parametrize("hours", [0, 0.1, -1])
def test_hours_to_slots_rejects_under_one_slot(hours):
    with pytest.raises(ValueError):
        hours_to_slots(hours)


def test_edit_changes_only_what_was_given():
    t = _task()
    edited = edit_extracted_task(t, hours=2, priority=5)
    assert (edited.duration_slots, edited.priority, edited.difficulty) == (8, 5, 3)
    assert (edited.title, edited.date) == ("HW", "2026-10-02")
    assert (t.duration_slots, t.priority) == (4, 3)  # original untouched


def test_edit_with_no_changes_returns_equal_task():
    assert edit_extracted_task(_task()) == _task()


@pytest.mark.parametrize("kwargs", [{"priority": 6}, {"priority": 0}, {"difficulty": 9}])
def test_edit_rejects_out_of_range(kwargs):
    with pytest.raises(ValidationError):
        edit_extracted_task(_task(), **kwargs)

def _extraction():
    return ExtractionResult(
        weekly_patterns=[WeeklyPattern(title="DS", day="Mon", start_time="09:00", end_time="11:00")],
        dated_blocks=[DatedBlock(title="Lab", date="2026-10-01", start_time="10:00", end_time="12:00")],
        tasks=[ExtractedTask(title="HW", date="2026-10-02")],
    )


def _run(answers, extraction=None):
    it = iter(answers)
    shown = []
    result = review_extraction(extraction or _extraction(),
                               ask=lambda _prompt: next(it), show=shown.append)
    return result, shown


def test_enter_accepts_everything_with_one_prompt():
    result, shown = _run([""])  # a single answer is all it takes
    assert result == _extraction()
    assert any("1." in s and "DS" in s for s in shown) and any("placeholder" in s for s in shown)


def test_empty_extraction_asks_nothing():
    result, _ = _run([], extraction=ExtractionResult())
    assert result == ExtractionResult()


def test_delete_picked_items_only():
    result, _ = _run(["1 3", "d", "d"])  # drop the pattern (1) and the task (3)
    assert result.weekly_patterns == [] and result.tasks == []
    assert [b.title for b in result.dated_blocks] == ["Lab"]


def test_edit_task_placeholders_enter_keeps_the_rest():
    # pick 3 -> edit -> title, date, hours=2, priority=5, difficulty (Enter keeps)
    result, _ = _run(["3", "e", "", "", "2", "5", ""])
    t = result.tasks[0]
    assert (t.title, t.date, t.duration_slots, t.priority, t.difficulty) == ("HW", "2026-10-02", 8, 5, 3)


def test_edit_weekly_pattern_normalises_day_and_time():
    result, _ = _run(["1", "e", "", "tuesday", "8:30", ""])
    p = result.weekly_patterns[0]
    assert (p.day, p.start_time, p.end_time) == ("Tue", "08:30", "11:00")


def test_edit_reasks_when_end_is_before_start():
    # first attempt: end 08:00 < start 09:00 -> pydantic error -> second attempt fixes it
    result, shown = _run(["1", "e", "", "", "", "08:00", "", "", "", "12:00"])
    assert result.weekly_patterns[0].end_time == "12:00"
    assert any("Invalid" in s for s in shown)


def test_edit_rejects_impossible_date():
    # bad date is rejected as soon as it's typed, then the edit restarts from the title
    result, shown = _run(["3", "e", "", "2026-13-45", "", "2026-11-01", "", "", ""])
    assert result.tasks[0].date == "2026-11-01"
    assert any("Invalid" in s for s in shown)


def test_out_of_range_or_junk_numbers_reask():
    result, shown = _run(["9", "abc", ""])
    assert result == _extraction()
    assert sum("Enter numbers" in s for s in shown) == 2


def test_junk_action_reasks_and_enter_leaves_item_alone():
    result, _ = _run(["1", "x", ""])
    assert result == _extraction()