import pytest
from scheduler.models import DynamicTask, FixedBlock, SLOTS_PER_DAY, SleepRule
from scheduler.solver import sleep_warnings, task_warnings, plan_day_cp, split_sizes, Exam, StudyPlanRule, generate_study_tasks

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

def test_generate_study_tasks_sizes_by_band():  # NEW
    exams = [Exam(title="Hard test", day=9, difficulty=5)]
    tasks = generate_study_tasks(exams)
    band = StudyPlanRule().hard
    assert tasks[0].duration_slots == round(band.min_hours_per_day * band.days_before * 4)
    assert tasks[0].max_session_slots == 8  # 2h default, independent of band.max_hours_per_day
    assert tasks[0].deadline_day == 9
    assert tasks[0].earliest_start_day == 9 - band.days_before

def test_generate_study_tasks_session_cap_can_be_overridden():  # NEW
    exams = [Exam(title="Test", day=9, difficulty=5)]
    tasks = generate_study_tasks(exams, max_session_slots=16)  # explicit 4h override
    assert tasks[0].max_session_slots == 16

def test_consecutive_study_sessions_prefer_different_days():  # NEW
    exams = [Exam(title="Hard test", day=8, difficulty=5)]  # 7-day window, plenty of room to spread
    tasks = generate_study_tasks(exams)
    items, unscheduled = plan_day_cp([], tasks, num_days=9)
    assert unscheduled == []
    days_used = sorted({i.day for i in items if i.kind == "task"})
    assert len(days_used) > 1  # not all crammed onto one or two days

def test_same_day_sessions_allowed_when_window_is_tight():
    # only 1 day of room -- both 2h sessions must double up, but 4h stays within the 4h/day cap
    exams = [Exam(title="Rushed", day=0, difficulty=1)]  # easy band, window collapses to day 0
    tasks = generate_study_tasks(exams)
    items, unscheduled = plan_day_cp([], tasks, num_days=1)
    assert unscheduled == []
    study_items = [i for i in items if i.kind == "task"]
    assert {i.day for i in study_items} == {0}

def test_daily_cap_drops_task_that_cannot_fit_even_doubled_up():
    # 5h needed in one day, but the cap is 4h/day -- must be dropped, not exceed the cap
    task = DynamicTask(title="Too much", duration_slots=20, priority=3, difficulty=2,
                       max_session_slots=8, max_daily_slots=16,
                       earliest_start_day=0, deadline_day=0)
    items, unscheduled = plan_day_cp([], [task], num_days=1)
    assert [t.title for t in unscheduled] == ["Too much"]

def test_daily_cap_ignored_when_not_set():
    # same 5h/one-day scenario, but no cap set -- it should fit fine
    task = DynamicTask(title="Fine", duration_slots=20, priority=3, difficulty=2,
                       max_session_slots=8, earliest_start_day=0, deadline_day=0)
    items, unscheduled = plan_day_cp([], [task], num_days=1)
    assert unscheduled == []

def test_generate_study_tasks_clamps_earliest_day_to_zero():  # NEW
    exams = [Exam(title="Soon", day=1, difficulty=5)]  # hard band wants 7 days before, but day 1 - 7 < 0
    tasks = generate_study_tasks(exams)
    assert tasks[0].earliest_start_day == 0

def test_study_task_does_not_start_before_its_window():  # NEW
    exams = [Exam(title="Test", day=6, difficulty=3)]  # medium: 5 days before -> earliest day 1
    tasks = generate_study_tasks(exams)
    items, unscheduled = plan_day_cp([], tasks, num_days=7)
    assert unscheduled == []
    for item in items:
        assert item.day >= 1

def test_study_task_finishes_before_the_exam():  # NEW
    exams = [Exam(title="Test", day=3, difficulty=1, slot=32)]  # easy, deadline day 3 slot 32
    tasks = generate_study_tasks(exams)
    items, unscheduled = plan_day_cp([], tasks, num_days=4)
    assert unscheduled == []
    for item in items:
        assert item.day * SLOTS_PER_DAY + item.end_slot <= 3 * SLOTS_PER_DAY + 32

def test_two_exams_generate_two_independent_study_tasks():  # NEW
    exams = [Exam(title="A", day=5, difficulty=1), Exam(title="B", day=10, difficulty=5)]
    tasks = generate_study_tasks(exams)
    items, unscheduled = plan_day_cp([], tasks, num_days=11)
    assert unscheduled == []
    titles = {i.title.split(" (")[0] for i in items}
    assert titles == {"Study: A", "Study: B"}

def test_time_limit_none_means_unlimited():
    # small, easy scenario -- should still solve fine with no cap at all
    tasks = [DynamicTask(title="A", duration_slots=4, priority=3, difficulty=2)]
    items, unscheduled = plan_day_cp([], tasks, time_limit_seconds=None)
    assert unscheduled == []

def test_time_limit_too_short_raises_clear_error():
    # large realistic scenario, near-zero time budget -- can't find any feasible plan in time
    fixed_blocks = []
    for day in range(14):
        fixed_blocks.append(FixedBlock(title="Course A", start_slot=32, end_slot=44, day=day))
        fixed_blocks.append(FixedBlock(title="Lab", start_slot=56, end_slot=64, day=day))
    exams = [Exam(title="Quiz 1", day=4, difficulty=1),
             Exam(title="Midterm A", day=9, difficulty=4),
             Exam(title="Midterm B", day=12, difficulty=5)]
    tasks = generate_study_tasks(exams) + [
        DynamicTask(title=f"Assignment {n}", duration_slots=8 + (n % 3) * 4,
                   priority=(n % 5) + 1, difficulty=(n % 5) + 1,
                   deadline_day=3 + n, deadline_slot=96)
        for n in range(10)
    ]
    sleep_rules = [SleepRule(night=n) for n in range(14)]
    with pytest.raises(RuntimeError, match="No schedule found within"):
        plan_day_cp(fixed_blocks, tasks, num_days=14, sleep_rules=sleep_rules,
                   time_limit_seconds=0.001)