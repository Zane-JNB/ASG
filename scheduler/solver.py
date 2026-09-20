import math
from ortools.sat.python import cp_model

from scheduler.models import DynamicTask, FixedBlock, ScheduledItem, SLOTS_PER_DAY

presence_bonus = 10_000  # big, so fitting a task always beats moving things earlier

def split_sizes(duration: int, max_session: int) -> list[int]:
    """Fewest, most even chunks that are each <= max_session."""
    n = math.ceil(duration / max_session)
    base, extra = divmod(duration, n)
    return [base + 1] * extra + [base] * (n - extra)

def plan_day_cp(fixed_blocks: list[FixedBlock], tasks: list[DynamicTask],buffer_slots: int = 1):
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
        # start = model.NewIntVar(0, SLOTS_PER_DAY - task.duration_slots, f"start_{i}")
        is_present = model.NewBoolVar(f"present_{i}")
        if task.splittable:
            sizes = split_sizes(task.duration_slots, task.max_session_slots)
        else:
            sizes = [task.duration_slots]
        chunks = []
        for j, size in enumerate(sizes):
            start = model.NewIntVar(0, SLOTS_PER_DAY - size, f"start_{i}_{j}")
            intervals.append(
                model.NewOptionalFixedSizeIntervalVar(
                    start, size + buffer_slots, is_present, f"task_{i}_{j}"
                )
            )
            if chunks:  # keep the parts in order
                prev_start, prev_size = chunks[-1]
                model.Add(
                    start >= prev_start + prev_size + buffer_slots
                ).OnlyEnforceIf(is_present)
            chunks.append((start, size))

        placed.append((task, is_present, chunks))

    # rule: nothing overlaps
    model.AddNoOverlap(intervals)

    # goal: fit as many high-priority tasks as possible, and place them early
    model.Maximize(
        sum(
            task.priority * presence_bonus * present - task.priority * sum(start for start, _ in chunks)
            for task, present, chunks in placed
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
    for task, present, chunks in placed:
        if not solver.Value(present):
            unscheduled.append(task)
            continue
        for j, (start, size) in enumerate(chunks):
            s = solver.Value(start)
            title = (
                task.title
                if len(chunks) == 1
                else f"{task.title} ({j + 1}/{len(chunks)})"
            )
            items.append(
                ScheduledItem(
                    title=title,
                    start_slot=s,
                    end_slot=s + size,
                    kind="task",
                )
            )
        # else:
        #     unscheduled.append(task)

    items.sort(key=lambda i: i.start_slot)
    return items, unscheduled

