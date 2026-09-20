from scheduler.models import (
    DynamicTask,
    FixedBlock,
    ScheduledItem,
    SLOTS_PER_DAY,
)

def build_free_map(fixed_blocks: list[FixedBlock]) -> list[bool]:
    """True = slot is free, False = slot is taken."""
    free_slot = [True] * SLOTS_PER_DAY
    for block in fixed_blocks:
        for slot in range(block.start_slot, block.end_slot):
            free_slot[slot] = False
    return free_slot

def find_first_fit(free_slot: list[bool], length: int) -> int | None:
    """Start slot of the first run of `length` free slots, or None."""
    run = 0
    for slot in range(SLOTS_PER_DAY):
        run = run + 1 if free_slot[slot] else 0
        if run == length:
            return slot - length + 1
    return None

def plan_day(fixed_blocks: list[FixedBlock], tasks: list[DynamicTask]):
    free = build_free_map(fixed_blocks)
    items = [
        ScheduledItem(
            title=b.title, start_slot=b.start_slot, end_slot=b.end_slot, kind="fixed"
        )
        for b in fixed_blocks
    ]
    unscheduled = []
    # highest priority first; ties broken by higher difficulty
    for task in sorted(tasks, key=lambda t: (-t.priority, -t.difficulty)):
        start = find_first_fit(free, task.duration_slots)
        if start is None:
            unscheduled.append(task)
            continue
        end = start + task.duration_slots
        for slot in range(start, end):
            free[slot] = False
        items.append(
            ScheduledItem(title=task.title, start_slot=start, end_slot=end, kind="task")
        )

    items.sort(key=lambda i: i.start_slot)
    return items, unscheduled

