import pytest
from scheduler.models import DynamicTask, FixedBlock, SLOTS_PER_DAY, SleepRule
from scheduler.solver import plan_day_cp
from scheduler.solver import split_sizes
from scheduler.solver import sleep_warnings, task_warnings

def test_solver_never_overlaps():
    blocks = [FixedBlock(title="Class", start_slot=8, end_slot=16)]
    tasks = [
        DynamicTask(title="A", duration_slots=6, priority=5, difficulty=3),
        DynamicTask(title="B", duration_slots=4, priority=2, difficulty=1),
    ]
    items, unscheduled = plan_day_cp(blocks, tasks)
    used = []
    for item in items:
        used.extend(range(item.start_slot, item.end_slot))
    assert len(used) == len(set(used))
    assert unscheduled == []


def test_solver_reports_tasks_that_do_not_fit():
    blocks = [FixedBlock(title="Everything", start_slot=0, end_slot=96)]
    tasks = [DynamicTask(title="A", duration_slots=4, priority=3, difficulty=2)]
    items, unscheduled = plan_day_cp(blocks, tasks)
    assert [t.title for t in unscheduled] == ["A"]

def test_solver_leaves_a_buffer_after_tasks():
    tasks = [
        DynamicTask(title="A", duration_slots=4, priority=5, difficulty=3),
        DynamicTask(title="B", duration_slots=4, priority=4, difficulty=3),
    ]
    items, _ = plan_day_cp([], tasks, buffer_slots=1)
    first, second = items[0], items[1]
    assert second.start_slot - first.end_slot >= 1

def test_split_sizes():
    assert split_sizes(12, 6) == [6, 6]
    assert split_sizes(13, 6) == [5, 4, 4]
    assert split_sizes(4, 8) == [4]


def test_long_task_is_split_in_order():
    task = DynamicTask(
        title="Long", duration_slots=12, priority=3, difficulty=3, max_session_slots=6
    )
    items, unscheduled = plan_day_cp([], [task])
    parts = [i for i in items if i.kind == "task"]
    assert len(parts) == 2
    assert sum(p.end_slot - p.start_slot for p in parts) == 12
    assert parts[0].end_slot <= parts[1].start_slot

def test_deadline_is_respected():
    task = DynamicTask(
        title="A", duration_slots=4, priority=3, difficulty=2,
        deadline_day=0, deadline_slot=20,
    )
    items, unscheduled = plan_day_cp([], [task], num_days=2)
    assert unscheduled == []
    assert items[0].day == 0 and items[0].end_slot <= 20

def test_impossible_deadline_leaves_task_unscheduled():
    block = FixedBlock(title="Busy", start_slot=0, end_slot=20, day=0)
    task = DynamicTask(
        title="A", duration_slots=4, priority=3, difficulty=2,
        deadline_day=0, deadline_slot=20,
    )
    items, unscheduled = plan_day_cp([block], [task], num_days=2)
    assert [t.title for t in unscheduled] == ["A"]

def test_task_can_cross_midnight():
    tasks = [
        DynamicTask(title=f"T{i}", duration_slots=40, priority=3, difficulty=2, splittable=False)
        for i in range(3)
    ]
    items, unscheduled = plan_day_cp([], tasks, num_days=2)
    assert unscheduled == []
    assert any(i.end_slot > SLOTS_PER_DAY for i in items)

def test_fixed_block_can_cross_midnight():
    night = FixedBlock(title="Sleep", start_slot=92, end_slot=SLOTS_PER_DAY + 28, day=0)
    task = DynamicTask(
        title="A", duration_slots=4, priority=3, difficulty=2, splittable=False
    )
    items, unscheduled = plan_day_cp([night], [task], num_days=2)
    part = [i for i in items if i.kind == "task"][0]
    start = part.day * SLOTS_PER_DAY + part.start_slot
    end = part.day * SLOTS_PER_DAY + part.end_slot
    assert end <= 92 or start >= SLOTS_PER_DAY + 28  # fully before or after the sleep

def sleep_items(items): 
    return [i for i in items if i.kind == "sleep"] 

def test_sleep_is_placed_at_preferred_bedtime_with_full_length(): 
    rule = SleepRule()  
    items, _ = plan_day_cp([], [], sleep_rules=[rule])  
    (sleep,) = sleep_items(items)  
    assert sleep.start_slot == rule.preferred_bed 
    assert sleep.end_slot - sleep.start_slot == rule.length_slots 
    assert sleep_warnings([rule], items) == []  

def test_tasks_do_not_overlap_sleep(): 
    tasks = [DynamicTask(title=f"T{i}", duration_slots=20, priority=3, difficulty=2,
                         splittable=False) for i in range(3)] 
    rule = SleepRule() 
    items, _ = plan_day_cp([], tasks, num_days=2, sleep_rules=[rule]) 
    used = [] 
    for item in items:
        used.extend(range(item.day * SLOTS_PER_DAY + item.start_slot,
                          item.day * SLOTS_PER_DAY + item.end_slot)) 
    assert len(used) == len(set(used)) 

def test_tasks_do_not_overlap_sleep(): 
    tasks = [DynamicTask(title=f"T{i}", duration_slots=20, priority=3, difficulty=2,
                         splittable=False) for i in range(3)] 
    rule = SleepRule() 
    items, _ = plan_day_cp([], tasks, num_days=2, sleep_rules=[rule])  
    used = []  
    for item in items: 
        used.extend(range(item.day * SLOTS_PER_DAY + item.start_slot,
                          item.day * SLOTS_PER_DAY + item.end_slot)) 
    assert len(used) == len(set(used)) 

def test_sleep_shortened_between_min_and_target_gives_soft_warning(): 
    rule = SleepRule(earliest_bed=88, preferred_bed=88, latest_bed=88,
                     length_slots=32, min_slots=24) 
    block = FixedBlock(title="Early flight", start_slot=18, end_slot=24, day=1) 
    items, _ = plan_day_cp([block], [], num_days=2, sleep_rules=[rule])  
    (sleep,) = sleep_items(items)   
    assert sleep.end_slot - sleep.start_slot == 26  
    warnings = sleep_warnings([rule], items)  
    assert [(w.severity, w.kind) for w in warnings] == [("soft", "sleep_short")]  

def test_sleep_below_minimum_gives_hard_warning_not_a_crash(): 
    rule = SleepRule(earliest_bed=88, preferred_bed=88, latest_bed=88,
                     length_slots=32, min_slots=24) 
    block = FixedBlock(title="Night shift", start_slot=4, end_slot=14, day=1)     
    items, _ = plan_day_cp([block], [], num_days=2, sleep_rules=[rule]) 
    warnings = sleep_warnings([rule], items) 
    assert any(w.severity == "hard" and w.kind == "sleep_short" for w in warnings)

def test_no_room_for_any_sleep_still_gives_hard_warning(): 
    rule = SleepRule(earliest_bed=88, preferred_bed=88, latest_bed=88) 
    block = FixedBlock(title="Exam", start_slot=88, end_slot=96)  
    items, _ = plan_day_cp([block], [], sleep_rules=[rule])  
    assert sleep_items(items) == []   
    warnings = sleep_warnings([rule], items)  
    assert [(w.severity, w.kind) for w in warnings] == [("hard", "sleep_short")]  

def test_skipped_sleep_frees_the_night_and_gives_hard_warning(): 
    tasks = [DynamicTask(title=f"T{i}", duration_slots=40, priority=3, difficulty=2,
                         splittable=False) for i in range(3)]  
    rule = SleepRule(skip=True)  
    items, unscheduled = plan_day_cp([], tasks, num_days=2, sleep_rules=[rule]) 
    assert sleep_items(items) == [] 
    assert unscheduled == []   
    assert any(i.end_slot > SLOTS_PER_DAY for i in items)   
    warnings = sleep_warnings([rule], items)   
    assert [(w.severity, w.kind) for w in warnings] == [("hard", "sleep_skipped")]  
 
def test_two_nights_each_get_their_own_sleep():   
    rules = [SleepRule(night=0), SleepRule(night=1)]  
    items, _ = plan_day_cp([], [], num_days=2, sleep_rules=rules)  
    assert len(sleep_items(items)) == 2   
    assert sleep_warnings(rules, items) == []   
 
def test_sleep_rule_for_a_night_outside_the_plan_is_rejected(): 
    with pytest.raises(ValueError):  
        plan_day_cp([], [], num_days=1, sleep_rules=[SleepRule(night=1)]) 
 
def test_two_rules_for_the_same_night_are_rejected():   
    with pytest.raises(ValueError):  
        plan_day_cp([], [], sleep_rules=[SleepRule(), SleepRule()])  
 
def test_missed_deadline_gives_hard_warning():  
    block = FixedBlock(title="Busy", start_slot=0, end_slot=20, day=0) 
    task = DynamicTask(title="A", duration_slots=4, priority=3, difficulty=2,
                       deadline_day=0, deadline_slot=20)  
    _, unscheduled = plan_day_cp([block], [task], num_days=2)  
    assert [(w.severity, w.kind) for w in task_warnings(unscheduled)] == [("hard", "task_unscheduled")]  