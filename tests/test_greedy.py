from scheduler.greedy import find_first_fit, plan_day
from scheduler.models import DynamicTask, FixedBlock


def test_find_first_fit_skips_too_small_gap():
    free = [True] * 96
    free[4] = False  # slots 0-3 free (only 4), slot 4 taken
    assert find_first_fit(free, 5) == 5


def test_plan_day_never_overlaps():
    blocks = [FixedBlock(title="Class", start_slot=8, end_slot=16)]
    tasks = [
        DynamicTask(title="A", duration_slots=6, priority=5, difficulty=3),
        DynamicTask(title="B", duration_slots=4, priority=2, difficulty=1),
    ]
    items, unscheduled = plan_day(blocks, tasks)
    used = []
    for item in items:
        used.extend(range(item.start_slot, item.end_slot))
    assert len(used) == len(set(used))  # no slot used twice
    assert unscheduled == []


def test_plan_day_reports_tasks_that_do_not_fit():
    blocks = [FixedBlock(title="Everything", start_slot=0, end_slot=96)]
    tasks = [DynamicTask(title="A", duration_slots=4, priority=3, difficulty=2)]
    items, unscheduled = plan_day(blocks, tasks)
    assert [t.title for t in unscheduled] == ["A"]