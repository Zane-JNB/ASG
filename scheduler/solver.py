from ortools.sat.python import cp_model

from scheduler.models import DynamicTask, FixedBlock, ScheduledItem, SLOTS_PER_DAY

presence_bonus = 10_000  # big, so fitting a task always beats moving things earlier


def plan_day_cp(fixed_blocks: list[FixedBlock], tasks: list[DynamicTask]):
    model = cp_model.CpModel()
    intervals = []

    # fixed blocks: intervals that can't move
    for block in fixed_blocks:
        length = block.end_slot - block.start_slot
        intervals.append(
            model.NewFixedSizeIntervalVar(block.start_slot, length, block.title)
        )

    # tasks: optional intervals the solver may place anywhere, or skip
    placed = []  # (task, start_var, is_present)
    for i, task in enumerate(tasks):
        start = model.NewIntVar(0, SLOTS_PER_DAY - task.duration_slots, f"start_{i}")
        is_present = model.NewBoolVar(f"present_{i}")
        intervals.append(
            model.NewOptionalFixedSizeIntervalVar(
                start, task.duration_slots, is_present, f"task_{i}"
            )
        )
        placed.append((task, start, is_present))

    # rule: nothing overlaps
    model.AddNoOverlap(intervals)

    # goal: fit as many high-priority tasks as possible, and place them early
    model.Maximize(
        sum(
            task.priority * presence_bonus * present - task.priority * start
            for task, start, present in placed
        )
    )

    solver = cp_model.CpSolver()
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError("No valid schedule (do your fixed blocks overlap?)")

    items = [
        ScheduledItem(
            title=b.title, start_slot=b.start_slot, end_slot=b.end_slot, kind="fixed"
        )
        for b in fixed_blocks
    ]
    unscheduled = []
    for task, start, present in placed:
        if solver.Value(present):
            s = solver.Value(start)
            items.append(
                ScheduledItem(
                    title=task.title,
                    start_slot=s,
                    end_slot=s + task.duration_slots,
                    kind="task",
                )
            )
        else:
            unscheduled.append(task)

    items.sort(key=lambda i: i.start_slot)
    return items, unscheduled