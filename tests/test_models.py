import pytest
from pydantic import ValidationError

from scheduler.models import DynamicTask, time_to_slot, slot_to_time

def test_time_to_slot():
    assert time_to_slot("00:00") == 0
    assert time_to_slot("09:30") == 38
    assert time_to_slot("23:45") == 95


def test_slot_to_time_roundtrip():
    assert slot_to_time(38) == "09:30"


def test_task_rejects_bad_priority():
    with pytest.raises(ValidationError):
        DynamicTask(title="x", duration_slots=4, priority=9, difficulty=3)