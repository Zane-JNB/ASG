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



# ---- extracted dates and times are checked before they can be saved ----
from scheduler.models import DatedBlock as _DB, ExtractedTask as _ET, WeeklyPattern as _WP

@pytest.mark.parametrize("start,end", [("24:00", "25:00"), ("9.30", "10:00"), ("09:60", "10:00"), ("09:00", "24:15")])
def test_bad_times_are_rejected(start, end):
    with pytest.raises(ValueError):
        _WP(title="x", day="Mon", start_time=start, end_time=end)

def test_a_class_may_end_at_midnight():
    p = _WP(title="Late lab", day="Mon", start_time="22:00", end_time="24:00")
    assert time_to_slot(p.end_time) == 96

@pytest.mark.parametrize("bad", ["Oct 12", "2026-02-30", ""])
def test_bad_dates_are_rejected(bad):
    with pytest.raises(ValueError):
        _ET(title="x", date=bad)
    with pytest.raises(ValueError):
        _DB(title="x", date=bad, start_time="09:00", end_time="10:00")

def test_dates_are_stored_in_one_sortable_form():
    assert _ET(title="x", date="20261005").date == "2026-10-05"
