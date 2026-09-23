import pytest
from pydantic import ValidationError
from scheduler.models import DynamicTask, time_to_slot, slot_to_time, FixedBlock, SleepRule, Exam, StudyPlanRule

def test_time_to_slot():
    assert time_to_slot("00:00") == 0
    assert time_to_slot("09:30") == 38
    assert time_to_slot("23:45") == 95

def test_slot_to_time_roundtrip():
    assert slot_to_time(38) == "09:30"

def test_task_rejects_bad_priority():
    with pytest.raises(ValidationError):
        DynamicTask(title="x", duration_slots=4, priority=9, difficulty=3)

def test_fixed_block_end_must_be_after_start():
    with pytest.raises(ValidationError):
        FixedBlock(title="x", start_slot=20, end_slot=10)

def test_sleep_rule_defaults_are_valid():  
    rule = SleepRule()   
    assert rule.min_slots <= rule.length_slots   
 
def test_sleep_rule_min_cannot_exceed_length():  
    with pytest.raises(ValidationError):  
        SleepRule(length_slots=20, min_slots=30) 
 
def test_sleep_rule_preferred_bed_must_be_in_window(): 
    with pytest.raises(ValidationError): 
        SleepRule(earliest_bed=88, preferred_bed=80, latest_bed=100)  

def test_study_plan_rule_bands_by_difficulty():
    rule = StudyPlanRule()
    assert rule.band_for(1) is rule.easy
    assert rule.band_for(2) is rule.easy
    assert rule.band_for(3) is rule.medium
    assert rule.band_for(4) is rule.hard
    assert rule.band_for(5) is rule.hard

def test_study_band_rejects_min_over_max():
    with pytest.raises(ValidationError):
        StudyPlanRule(easy=dict(days_before=2, min_hours_per_day=5, max_hours_per_day=3))

def test_earliest_start_defaults_to_none():  
    task = DynamicTask(title="x", duration_slots=4, priority=1, difficulty=1)
    assert task.earliest_start_day is None

