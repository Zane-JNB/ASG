import math
from ortools.sat.python import cp_model
from scheduler.models import DynamicTask, FixedBlock, ScheduledItem, SLOTS_PER_DAY

presence_bonus = 10_000  # big, so fitting a task always beats moving things earlier

def split_sizes(duration: int, max_session: int) -> list[int]:
    """Fewest, most even chunks that are each <= max_session."""
    n = math.ceil(duration / max_session)
    base, extra = divmod(duration, n)
    return [base + 1] * extra + [base] * (n - extra)

def plan_day_cp(fixed_blocks: list[FixedBlock], tasks: list[DynamicTask],
                buffer_slots: int = 1, num_days: int = 1):
    
    model = cp_model.CpModel()
    intervals = []

    # fixed blocks: intervals that can't move
    for block in fixed_blocks:
        if block.day >= num_days:
            raise ValueError(
                f"'{block.title}' is on day {block.day}, but the plan has {num_days} day(s)"
            )
        length = block.end_slot - block.start_slot
        start = block.day * SLOTS_PER_DAY + block.start_slot
        intervals.append(model.NewFixedSizeIntervalVar(start, length, block.title))

    # tasks: each becomes one or more chunks the solver places
    placed = []  # (task, is_present, [(start_expr, size), ...])
    for i, task in enumerate(tasks):
        is_present = model.NewBoolVar(f"present_{i}")

        if task.splittable:
            sizes = split_sizes(task.duration_slots, task.max_session_slots)
        else:
            sizes = [task.duration_slots]

        chunks = []
        for j, size in enumerate(sizes):
            start = model.NewIntVar(0, num_days * SLOTS_PER_DAY - size, f"start_{i}_{j}")

            intervals.append(
                model.NewOptionalFixedSizeIntervalVar(
                    start, size + buffer_slots, is_present, f"task_{i}_{j}"
                )
            )

            # rule: finish before the deadline
            if task.deadline_day is not None:
                deadline_end = task.deadline_day * SLOTS_PER_DAY + task.deadline_slot
                model.Add(start + size <= deadline_end).OnlyEnforceIf(is_present)

            # rule: parts of one task stay in order
            if chunks:
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
            task.priority * presence_bonus * present
            - task.priority * sum(start for start, _ in chunks)
            for task, present, chunks in placed
        )
    )

    solver = cp_model.CpSolver()
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError("No valid schedule (do your fixed blocks overlap?)")

    items = [
        ScheduledItem(
            title=b.title,
            start_slot=b.start_slot,
            end_slot=b.end_slot,
            kind="fixed",
            day=b.day,
        )
        for b in fixed_blocks
    ]
    unscheduled = []
    for task, present, chunks in placed:
        if not solver.Value(present):
            unscheduled.append(task)
            continue
        for j, (start, size) in enumerate(chunks):
            day, offset = divmod(solver.Value(start), SLOTS_PER_DAY)
            title = (
                task.title
                if len(chunks) == 1
                else f"{task.title} ({j + 1}/{len(chunks)})"
            )
            items.append(
                ScheduledItem(
                    title=title,
                    start_slot=offset,
                    end_slot=offset + size,
                    kind="task",
                    day=day,
                )
            )

    items.sort(key=lambda i: (i.day, i.start_slot))
    return items, unscheduled