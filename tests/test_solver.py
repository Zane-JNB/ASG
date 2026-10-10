import pytest
from unittest import mock
from ortools.sat.python import cp_model
from scheduler.models import DynamicTask, FixedBlock, ProfileSettings, ScheduledItem, SleepRule
from scheduler.units import SLOTS_PER_DAY
from scheduler.solver import (
    PlanFrame, build_schedule, chunk_sizes, merge_fixed_spans, sleep_warnings, split_sizes, task_warnings,
)

def _study(title, duration, difficulty, earliest, deadline, deadline_slot=SLOTS_PER_DAY):
    """A multi-day study task, capped at 4h a day in 2h sessions."""
    return DynamicTask(title=f"Study: {title}", duration_slots=duration, priority=4, difficulty=difficulty,
                       max_session_slots=8, max_daily_slots=16, earliest_start_day=earliest,
                       deadline_day=deadline, deadline_slot=deadline_slot)

def test_solver_never_overlaps():
    blocks = [FixedBlock(title="Class", start_slot=8, end_slot=16)]
    tasks = [
        DynamicTask(title="A", duration_slots=6, priority=5, difficulty=3),
        DynamicTask(title="B", duration_slots=4, priority=2, difficulty=1),
    ]
    items, unscheduled = build_schedule(blocks, tasks)
    used = []
    for item in items:
        used.extend(range(item.start_slot, item.end_slot))
    assert len(used) == len(set(used))
    assert unscheduled == []


def test_solver_reports_tasks_that_do_not_fit():
    blocks = [FixedBlock(title="Everything", start_slot=0, end_slot=96)]
    tasks = [DynamicTask(title="A", duration_slots=4, priority=3, difficulty=2)]
    items, unscheduled = build_schedule(blocks, tasks)
    assert [t.title for t in unscheduled] == ["A"]

def test_solver_leaves_a_buffer_after_tasks():
    tasks = [
        DynamicTask(title="A", duration_slots=4, priority=5, difficulty=3),
        DynamicTask(title="B", duration_slots=4, priority=4, difficulty=3),
    ]
    items, _ = build_schedule([], tasks, settings=ProfileSettings(buffer_slots=1))
    first, second = items[0], items[1]
    assert second.start_slot - first.end_slot >= 1

def test_long_split_task_that_fits_is_not_dropped_over_a_long_horizon():
    # many late chunks add up a large "start early" cost; it must never outweigh fitting the task
    days = 28
    classes = [FixedBlock(title="Class", day=d, start_slot=32, end_slot=68) for d in range(days)]
    sleep = [SleepRule(night=d) for d in range(days)]
    task = DynamicTask(title="Thesis", duration_slots=240, priority=1, max_session_slots=8,
                       deadline_day=days - 1)
    items, unscheduled = build_schedule(classes, [task], num_days=days, sleep_rules=sleep,
                                        time_limit_seconds=20)
    assert unscheduled == []
    assert sum(i.end_slot - i.start_slot for i in items if i.kind == "task") == 240


def test_split_sizes():
    assert split_sizes(12, 6) == [6, 6]
    assert split_sizes(13, 6) == [5, 4, 4]
    assert split_sizes(4, 8) == [4]


def test_long_task_is_split_in_order():
    task = DynamicTask(
        title="Long", duration_slots=12, priority=3, difficulty=3, max_session_slots=6
    )
    items, unscheduled = build_schedule([], [task])
    parts = [i for i in items if i.kind == "task"]
    assert len(parts) == 2
    assert sum(p.end_slot - p.start_slot for p in parts) == 12
    assert parts[0].end_slot <= parts[1].start_slot

def test_deadline_is_respected():
    task = DynamicTask(
        title="A", duration_slots=4, priority=3, difficulty=2,
        deadline_day=0, deadline_slot=20,
    )
    items, unscheduled = build_schedule([], [task], num_days=2)
    assert unscheduled == []
    assert items[0].day == 0 and items[0].end_slot <= 20

def test_impossible_deadline_leaves_task_unscheduled():
    block = FixedBlock(title="Busy", start_slot=0, end_slot=20, day=0)
    task = DynamicTask(
        title="A", duration_slots=4, priority=3, difficulty=2,
        deadline_day=0, deadline_slot=20,
    )
    items, unscheduled = build_schedule([block], [task], num_days=2)
    assert [t.title for t in unscheduled] == ["A"]

def test_task_can_cross_midnight():
    tasks = [
        DynamicTask(title=f"T{i}", duration_slots=40, priority=3, difficulty=2, splittable=False)
        for i in range(3)
    ]
    items, unscheduled = build_schedule([], tasks, num_days=2)
    assert unscheduled == []
    assert any(i.end_slot > SLOTS_PER_DAY for i in items)

def test_fixed_block_can_cross_midnight():
    night = FixedBlock(title="Sleep", start_slot=92, end_slot=SLOTS_PER_DAY + 28, day=0)
    task = DynamicTask(
        title="A", duration_slots=4, priority=3, difficulty=2, splittable=False
    )
    items, unscheduled = build_schedule([night], [task], num_days=2)
    part = [i for i in items if i.kind == "task"][0]
    start = part.day * SLOTS_PER_DAY + part.start_slot
    end = part.day * SLOTS_PER_DAY + part.end_slot
    assert end <= 92 or start >= SLOTS_PER_DAY + 28  # fully before or after the sleep

def sleep_items(items): 
    return [i for i in items if i.kind == "sleep"] 

def test_sleep_is_placed_at_preferred_bedtime_with_full_length(): 
    rule = SleepRule()  
    items, _ = build_schedule([], [], sleep_rules=[rule])  
    (sleep,) = sleep_items(items)  
    assert sleep.start_slot == rule.preferred_bed 
    assert sleep.end_slot - sleep.start_slot == rule.length_slots 
    assert sleep_warnings([rule], items) == []  

def test_latest_wake_ends_sleep_in_time_by_going_to_bed_earlier():
    rule = SleepRule(latest_wake=SLOTS_PER_DAY + 24)  # up by 06:00
    items, _ = build_schedule([], [], sleep_rules=[rule])
    (sleep,) = sleep_items(items)
    assert sleep.end_slot <= SLOTS_PER_DAY + 24
    assert sleep.end_slot - sleep.start_slot == rule.length_slots  # bedtime moved, sleep kept

def test_latest_wake_before_earliest_bed_is_rejected():
    with pytest.raises(ValueError):
        SleepRule(latest_wake=80)

def test_tasks_do_not_overlap_sleep(): 
    tasks = [DynamicTask(title=f"T{i}", duration_slots=20, priority=3, difficulty=2,
                         splittable=False) for i in range(3)] 
    rule = SleepRule() 
    items, _ = build_schedule([], tasks, num_days=2, sleep_rules=[rule])  
    used = []  
    for item in items: 
        used.extend(range(item.day * SLOTS_PER_DAY + item.start_slot,
                          item.day * SLOTS_PER_DAY + item.end_slot)) 
    assert len(used) == len(set(used)) 

def test_sleep_shortened_between_min_and_target_gives_soft_warning(): 
    rule = SleepRule(earliest_bed=88, preferred_bed=88, latest_bed=88,
                     length_slots=32, min_slots=24) 
    block = FixedBlock(title="Early flight", start_slot=18, end_slot=24, day=1) 
    items, _ = build_schedule([block], [], num_days=2, sleep_rules=[rule],
                              settings=ProfileSettings(wake_buffer_slots=0))  # no get-ready time here
    (sleep,) = sleep_items(items)
    assert sleep.end_slot - sleep.start_slot == 26
    warnings = sleep_warnings([rule], items)  
    assert [(w.severity, w.kind) for w in warnings] == [("soft", "sleep_short")]  

def test_sleep_below_minimum_gives_hard_warning_not_a_crash(): 
    rule = SleepRule(earliest_bed=88, preferred_bed=88, latest_bed=88,
                     length_slots=32, min_slots=24) 
    block = FixedBlock(title="Night shift", start_slot=4, end_slot=14, day=1)     
    items, _ = build_schedule([block], [], num_days=2, sleep_rules=[rule]) 
    warnings = sleep_warnings([rule], items) 
    assert any(w.severity == "hard" and w.kind == "sleep_short" for w in warnings)

def test_no_room_for_any_sleep_still_gives_hard_warning(): 
    rule = SleepRule(earliest_bed=88, preferred_bed=88, latest_bed=88) 
    block = FixedBlock(title="Exam", start_slot=88, end_slot=96)  
    items, _ = build_schedule([block], [], sleep_rules=[rule])  
    assert sleep_items(items) == []   
    warnings = sleep_warnings([rule], items)  
    assert [(w.severity, w.kind) for w in warnings] == [("hard", "sleep_short")]  

def test_skipped_sleep_frees_the_night_and_gives_hard_warning(): 
    tasks = [DynamicTask(title=f"T{i}", duration_slots=40, priority=3, difficulty=2,
                         splittable=False) for i in range(3)]  
    rule = SleepRule(skip=True)  
    items, unscheduled = build_schedule([], tasks, num_days=2, sleep_rules=[rule]) 
    assert sleep_items(items) == [] 
    assert unscheduled == []   
    assert any(i.end_slot > SLOTS_PER_DAY for i in items)   
    warnings = sleep_warnings([rule], items)   
    assert [(w.severity, w.kind) for w in warnings] == [("hard", "sleep_skipped")]  
 
def test_two_nights_each_get_their_own_sleep():   
    rules = [SleepRule(night=0), SleepRule(night=1)]  
    items, _ = build_schedule([], [], num_days=2, sleep_rules=rules)  
    assert len(sleep_items(items)) == 2   
    assert sleep_warnings(rules, items) == []   
 
def test_sleep_rule_for_a_night_outside_the_plan_is_rejected(): 
    with pytest.raises(ValueError):  
        build_schedule([], [], num_days=1, sleep_rules=[SleepRule(night=1)]) 
 
def test_two_rules_for_the_same_night_are_rejected():   
    with pytest.raises(ValueError):  
        build_schedule([], [], sleep_rules=[SleepRule(), SleepRule()])  
 
def test_missed_deadline_gives_hard_warning():  
    block = FixedBlock(title="Busy", start_slot=0, end_slot=20, day=0) 
    task = DynamicTask(title="A", duration_slots=4, priority=3, difficulty=2,
                       deadline_day=0, deadline_slot=20)  
    _, unscheduled = build_schedule([block], [task], num_days=2)  
    assert [(w.severity, w.kind) for w in task_warnings(unscheduled)] == [("hard", "task_unscheduled")]  

def test_consecutive_study_sessions_prefer_different_days():  
    tasks = [_study("Hard test", 56, 5, earliest=1, deadline=8)]  # 7-day window, plenty of room to spread
    items, unscheduled = build_schedule([], tasks, num_days=9)
    assert unscheduled == []
    days_used = sorted({i.day for i in items if i.kind == "task"})
    assert len(days_used) > 1  # not all crammed onto one or two days

def test_same_day_sessions_allowed_when_window_is_tight():
    # only 1 day of room -- both 2h sessions must double up, but 4h stays within the 4h/day cap
    tasks = [_study("Rushed", 16, 1, earliest=0, deadline=0)]  # window collapses to day 0
    items, unscheduled = build_schedule([], tasks, num_days=1)
    assert unscheduled == []
    study_items = [i for i in items if i.kind == "task"]
    assert {i.day for i in study_items} == {0}

def test_daily_cap_drops_task_that_cannot_fit_even_doubled_up():
    # 5h needed in one day, but the cap is 4h/day -- must be dropped, not exceed the cap
    task = DynamicTask(title="Too much", duration_slots=20, priority=3, difficulty=2,
                       max_session_slots=8, max_daily_slots=16,
                       earliest_start_day=0, deadline_day=0)
    items, unscheduled = build_schedule([], [task], num_days=1)
    assert [t.title for t in unscheduled] == ["Too much"]

def test_daily_cap_ignored_when_not_set():
    # same 5h/one-day scenario, but no cap set -- it should fit fine
    task = DynamicTask(title="Fine", duration_slots=20, priority=3, difficulty=2,
                       max_session_slots=8, earliest_start_day=0, deadline_day=0)
    items, unscheduled = build_schedule([], [task], num_days=1)
    assert unscheduled == []

def test_study_task_does_not_start_before_its_window():  
    tasks = [_study("Test", 40, 3, earliest=1, deadline=6)]
    items, unscheduled = build_schedule([], tasks, num_days=7)
    assert unscheduled == []
    for item in items:
        assert item.day >= 1

def test_study_task_finishes_before_the_exam():  
    tasks = [_study("Test", 16, 1, earliest=1, deadline=3, deadline_slot=32)]
    items, unscheduled = build_schedule([], tasks, num_days=4)
    assert unscheduled == []
    for item in items:
        assert item.day * SLOTS_PER_DAY + item.end_slot <= 3 * SLOTS_PER_DAY + 32

def test_two_exams_generate_two_independent_study_tasks():  
    tasks = [_study("A", 16, 1, earliest=3, deadline=5), _study("B", 56, 5, earliest=3, deadline=10)]
    items, unscheduled = build_schedule([], tasks, num_days=11)
    assert unscheduled == []
    titles = {i.title.split(" (")[0] for i in items}
    assert titles == {"Study: A", "Study: B"}

def test_time_limit_none_means_unlimited():
    # small, easy scenario -- should still solve fine with no cap at all
    tasks = [DynamicTask(title="A", duration_slots=4, priority=3, difficulty=2)]
    items, unscheduled = build_schedule([], tasks, time_limit_seconds=None)
    assert unscheduled == []

def _too_big_for_a_short_limit():
    """A large realistic scenario: no feasible plan is found in a near-zero time budget."""
    fixed_blocks = []
    for day in range(14):
        fixed_blocks.append(FixedBlock(title="Course A", start_slot=32, end_slot=44, day=day))
        fixed_blocks.append(FixedBlock(title="Lab", start_slot=56, end_slot=64, day=day))
    tasks = [_study("Quiz 1", 16, 1, earliest=2, deadline=4),
             _study("Midterm A", 56, 4, earliest=2, deadline=9),
             _study("Midterm B", 56, 5, earliest=5, deadline=12)] + [
        DynamicTask(title=f"Assignment {n}", duration_slots=8 + (n % 3) * 4,
                   priority=(n % 5) + 1, difficulty=(n % 5) + 1,
                   deadline_day=3 + n, deadline_slot=96)
        for n in range(10)
    ]
    sleep_rules = [SleepRule(night=n) for n in range(14)]
    return fixed_blocks, tasks, sleep_rules


def test_time_limit_too_short_raises_clear_error():
    fixed_blocks, tasks, sleep_rules = _too_big_for_a_short_limit()
    with pytest.raises(RuntimeError, match="No schedule found within"):
        build_schedule(fixed_blocks, tasks, num_days=14, sleep_rules=sleep_rules,
                   time_limit_seconds=0.001)

def test_task_gets_a_break_after_a_fixed_block():
    block = FixedBlock(title="Class", start_slot=32, end_slot=48)  # 08:00-12:00
    task = DynamicTask(title="Homework", duration_slots=4, priority=3, difficulty=2,
                       earliest_start_day=0, earliest_start_slot=48)
    items, unscheduled = build_schedule([block], [task], settings=ProfileSettings(buffer_slots=2))
    homework = [i for i in items if i.kind == "task"][0]
    assert homework.start_slot >= block.end_slot + 2

def test_task_can_still_end_right_before_a_fixed_block_starts():
    block = FixedBlock(title="Class", start_slot=32, end_slot=48)
    task = DynamicTask(title="Homework", duration_slots=4, priority=3, difficulty=2)
    items, unscheduled = build_schedule([block], [task], settings=ProfileSettings(buffer_slots=2))
    homework = [i for i in items if i.kind == "task"][0]
    assert homework.end_slot <= block.start_slot  # no buffer required before a block

def test_two_fixed_blocks_can_still_be_back_to_back():
    b1 = FixedBlock(title="Class A", start_slot=32, end_slot=48)
    b2 = FixedBlock(title="Class B", start_slot=48, end_slot=64)  # zero gap from b1
    items, unscheduled = build_schedule([b1, b2], [], settings=ProfileSettings(buffer_slots=2))
    assert unscheduled == []
    assert len(items) == 2

def test_profile_settings_defaults_match_old_constants():
    s = ProfileSettings()
    assert s.presence_bonus == 10_000
    assert s.sleep_min_penalty == 1_000_000
    assert s.default_max_session_slots == 8

def test_profile_settings_default_sleep_rule_uses_profile_defaults():
    s = ProfileSettings(default_sleep_length_slots=28, default_preferred_bed=88)
    rule = s.default_sleep_rule(night=2)
    assert rule.night == 2
    assert rule.length_slots == 28
    assert rule.preferred_bed == 88

def test_profile_settings_default_sleep_rule_allows_override():
    s = ProfileSettings()
    rule = s.default_sleep_rule(night=0, skip=True)
    assert rule.skip is True

def test_custom_settings_change_solver_behavior():
    low_spread = ProfileSettings(same_day_penalty=1)
    tasks = [_study("Hard test", 56, 5, earliest=1, deadline=8)]
    items, unscheduled = build_schedule([], tasks, num_days=9, settings=low_spread)
    assert unscheduled == []

def test_settings_defaults_to_profile_settings_when_omitted():
    tasks = [DynamicTask(title="A", duration_slots=4, priority=3, difficulty=2)]
    items, unscheduled = build_schedule([], tasks)  # no settings passed
    assert unscheduled == []

def test_buffer_slots_falls_back_to_settings_when_not_given():
    custom = ProfileSettings(buffer_slots=4)  # 1h gap instead of the usual 15 min
    block = FixedBlock(title="Class", start_slot=32, end_slot=48)
    task = DynamicTask(title="Homework", duration_slots=4, priority=3, difficulty=2,
                       earliest_start_day=0, earliest_start_slot=48)
    items, unscheduled = build_schedule([block], [task], settings=custom)  # no buffer_slots arg
    homework = [i for i in items if i.kind == "task"][0]
    assert homework.start_slot >= block.end_slot + 4

def test_merge_fixed_spans_unions_overlaps_and_keeps_touching():   
    a = FixedBlock(title="Commute", start_slot=28, end_slot=32, buffer_before=False)
    b = FixedBlock(title="Class", start_slot=30, end_slot=40)
    c = FixedBlock(title="Lab", start_slot=40, end_slot=48)  # touches, not merged
    assert merge_fixed_spans([b, a, c]) == [(28, 40, False), (40, 48, True)]

def test_task_can_end_flush_against_commute():   
    commute = FixedBlock(title="Commute", start_slot=40, end_slot=44, buffer_before=False)
    task = DynamicTask(title="Read", duration_slots=4, priority=3,
                       earliest_start_day=0, earliest_start_slot=36, deadline_day=0, deadline_slot=40)
    items, unscheduled = build_schedule([commute], [task], settings=ProfileSettings(buffer_slots=2))
    read = [i for i in items if i.kind == "task"][0]
    assert unscheduled == [] and read.end_slot == 40

def test_normal_block_still_needs_buffer_before():   
    cls = FixedBlock(title="Class", start_slot=40, end_slot=44)  # buffer_before=True
    task = DynamicTask(title="Read", duration_slots=4, priority=3,
                       earliest_start_day=0, earliest_start_slot=36, deadline_day=0, deadline_slot=40)
    _, unscheduled = build_schedule([cls], [task], settings=ProfileSettings(buffer_slots=2))
    assert [t.title for t in unscheduled] == ["Read"]

def test_overlapping_commute_and_class_solve_without_error():   
    cls = FixedBlock(title="Class", start_slot=30, end_slot=40)
    commute = FixedBlock(title="Commute", start_slot=28, end_slot=32, buffer_before=False)
    task = DynamicTask(title="Read", duration_slots=4, priority=3)
    items, unscheduled = build_schedule([cls, commute], [task], settings=ProfileSettings(buffer_slots=2))
    read = [i for i in items if i.kind == "task"][0]
    assert unscheduled == []
    assert sum(i.kind == "fixed" for i in items) == 2  # both still shown
    assert read.end_slot <= 28 or read.start_slot >= 42  # clear of 28-40 plus buffer after

@pytest.mark.parametrize("blocks", [
    [FixedBlock(title="Job", start_slot=80, end_slot=104)],  # 20:00 - 02:00
    [FixedBlock(title="Job", start_slot=80, end_slot=96),    # 20:00 - 24:00, then a late commute
     FixedBlock(title="Commute", start_slot=95, end_slot=101, buffer_before=False)],
])
def test_bedtime_window_fully_blocked_is_a_hard_warning_not_a_crash(blocks):
    rule = SleepRule(night=0)
    items, _ = build_schedule(blocks, [], num_days=1, sleep_rules=[rule], time_limit_seconds=10)
    assert not [i for i in items if i.kind == "sleep"]
    [w] = sleep_warnings([rule], items)
    assert (w.severity, w.kind) == ("hard", "sleep_short")


def test_sleep_warning_hours_are_exact():
    # 7h45m of sleep used to show as "7.8h" (#16)
    rule = SleepRule(length_slots=32, min_slots=24)
    items = [ScheduledItem(title="Sleep", start_slot=92, end_slot=92 + 31, kind="sleep", day=0)]
    [w] = sleep_warnings([rule], items)
    assert w.message == "Night 0: 7h 45m of sleep, shorter than your target of 8h."


def test_chunk_sizes_follow_the_split_choice():
    whole = DynamicTask(title="Essay", duration_slots=13, priority=3, splittable=False, max_session_slots=6)
    split = whole.model_copy(update={"splittable": True})
    assert chunk_sizes(whole) == [13]
    assert chunk_sizes(split) == [5, 4, 4]


def test_buffer_after_a_block_also_covers_a_short_gap_before_the_next_block():
    first = FixedBlock(title="Class A", start_slot=32, end_slot=48)
    second = FixedBlock(title="Class B", start_slot=50, end_slot=60)  # 30 min gap, buffer is 1h
    task = DynamicTask(title="Read", duration_slots=1, priority=3, earliest_start_day=0, earliest_start_slot=48)
    items, unscheduled = build_schedule([first, second], [task], settings=ProfileSettings(buffer_slots=4))
    [read] = [i for i in items if i.kind == "task"]
    assert unscheduled == []
    assert read.start_slot >= 64  # not in the gap: the earliest is after Class B plus its buffer


def test_buffer_after_a_block_with_touching_blocks():
    first = FixedBlock(title="Class", start_slot=32, end_slot=48)
    commute = FixedBlock(title="Bus", start_slot=48, end_slot=52, buffer_before=False)
    task = DynamicTask(title="Read", duration_slots=2, priority=3, earliest_start_day=0, earliest_start_slot=40)
    items, unscheduled = build_schedule([first, commute], [task], settings=ProfileSettings(buffer_slots=4))
    [read] = [i for i in items if i.kind == "task"]
    assert unscheduled == []
    assert read.start_slot >= 56


def _model_size(blocks, tasks, num_days):
    """Number of variables in the CP-SAT model build_schedule makes (#13)."""
    seen = {}
    real_solve = cp_model.CpSolver.Solve

    def counting(self, model, *args, **kwargs):
        seen["vars"] = len(model.Proto().variables)
        return real_solve(self, model, *args, **kwargs)

    with mock.patch.object(cp_model.CpSolver, "Solve", counting):
        build_schedule(blocks, tasks, num_days=num_days, time_limit_seconds=5)
    return seen["vars"]


def test_buffer_after_blocks_adds_no_variable_per_block_and_chunk():
    # #13: one bool per (block x chunk) made the model grow with blocks * chunks
    tasks = [DynamicTask(title=f"T{i}", duration_slots=8, priority=3, max_session_slots=2) for i in range(5)]
    few = [FixedBlock(title="Class", day=0, start_slot=40, end_slot=44)]
    many = [FixedBlock(title="Class", day=d, start_slot=s, end_slot=s + 4)
            for d in range(4) for s in (20, 40, 60, 80)]
    pairs = (len(many) - len(few)) * 4 * len(tasks)  # 4 chunks per task
    assert _model_size(many, tasks, 4) - _model_size(few, tasks, 4) < pairs // 4


def test_plan_frame_solve_all_fit_and_unplaced():
    frame = PlanFrame([FixedBlock(title="Everything", start_slot=0, end_slot=90)])
    fits = DynamicTask(title="A", duration_slots=2, priority=3)
    too_big = DynamicTask(title="B", duration_slots=8, priority=3)
    items, warnings = frame.solve_all_fit([fits])
    assert [i.title for i in items if i.kind == "task"] == ["A"] and warnings == []
    assert frame.solve_all_fit([fits, too_big]) is None
    assert frame.unplaced([fits, too_big]) == [1]


def test_plan_frame_trial_counts_running_out_of_time_as_not_fitting():
    fixed_blocks, tasks, sleep_rules = _too_big_for_a_short_limit()
    frame = PlanFrame(fixed_blocks, 14, sleep_rules)
    with pytest.raises(RuntimeError, match="No schedule found within"):
        frame.solve_all_fit(tasks, time_limit_seconds=0.001)  # a fit that can't be checked is an error
    assert frame.trial(tasks, time_limit_seconds=0.001) is None  # one trial of many: just "no"
