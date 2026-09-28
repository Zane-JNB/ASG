import pytest
from pydantic import ValidationError

from scheduler.models import ExtractedTask
from scheduler.review import hours_to_slots, edit_extracted_task


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