from scheduler.models import DynamicTask, FixedBlock, SLOTS_PER_DAY
from scheduler.solver import plan_day_cp
from scheduler.solver import split_sizes

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