from datetime import date, timedelta

from scheduler.models import FixedBlock, DynamicTask, WeeklyPattern, ExtractedTask, SLOTS_PER_DAY, time_to_slot,DatedBlock

_WEEKDAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]  # matches date.weekday()'s 0-6 order


def weekday_name(d: date) -> str:
    """'Mon'..'Sun' for a real date, matching WeeklyPattern.days_of_week's values."""
    return _WEEKDAY_ABBR[d.weekday()]


def day_index_for_date(plan_start_date: date, target_date: date) -> int:
    """How many days after plan_start_date target_date falls -- this app's 'day' number.

    Raises if target_date is before the plan starts, since there's no negative day 'day=-1'
    in this system -- a deadline before the plan begins can't be represented.
    """
    delta = (target_date - plan_start_date).days
    if delta < 0:
        raise ValueError(
            f"{target_date.isoformat()} is before the plan's start date "
            f"({plan_start_date.isoformat()}) -- it can't be represented as a day index"
        )
    return delta


def expand_weekly_pattern(pattern: WeeklyPattern, plan_start_date: date, num_days: int) -> list[FixedBlock]:
    """Turn one recurring (single-day) pattern into a concrete FixedBlock for each matching day
    in the plan -- e.g. a Monday pattern over a 14-day plan produces two FixedBlocks.
    """
    start_slot = time_to_slot(pattern.start_time)
    end_slot = time_to_slot(pattern.end_time)

    blocks = []
    for day_index in range(num_days):
        current_date = plan_start_date + timedelta(days=day_index)
        if weekday_name(current_date) == pattern.day:
            blocks.append(FixedBlock(
                title=pattern.title, start_slot=start_slot, end_slot=end_slot, day=day_index,
            ))
    return blocks


def expand_weekly_patterns(patterns: list[WeeklyPattern], plan_start_date: date,
                           num_days: int) -> list[FixedBlock]:
    """Convenience: expand a whole list of patterns at once."""
    blocks = []
    for pattern in patterns:
        blocks.extend(expand_weekly_pattern(pattern, plan_start_date, num_days))
    return blocks


def extracted_task_to_dynamic_task(task: ExtractedTask, plan_start_date: date) -> DynamicTask:
    """Convert one extracted task (real calendar date) into a DynamicTask (relative day index)."""
    deadline_date = date.fromisoformat(task.date)
    deadline_day = day_index_for_date(plan_start_date, deadline_date)
    return DynamicTask(
        title=task.title,
        duration_slots=task.duration_slots,
        priority=task.priority,
        difficulty=task.difficulty,
        deadline_day=deadline_day,
        deadline_slot=SLOTS_PER_DAY,
    )


def extracted_tasks_to_dynamic_tasks(tasks: list[ExtractedTask], plan_start_date: date) -> list[DynamicTask]:
    """Convenience: convert a whole list at once. Tasks whose date is before the plan's start
    are skipped rather than raising, since a document may legitimately list past deadlines
    that are no longer relevant to a plan starting today.
    """
    result = []
    for task in tasks:
        try:
            result.append(extracted_task_to_dynamic_task(task, plan_start_date))
        except ValueError:
            continue
    return result

def dated_block_to_fixed_block(block: DatedBlock, plan_start_date: date) -> FixedBlock:
    """Convert one dated, specific-time block (a real calendar date + start/end time) into a
    FixedBlock for a specific plan.
    """
    block_date = date.fromisoformat(block.date)
    day_index = day_index_for_date(plan_start_date, block_date)
    return FixedBlock(
        title=block.title,
        start_slot=time_to_slot(block.start_time),
        end_slot=time_to_slot(block.end_time),
        day=day_index,
    )


def dated_blocks_to_fixed_blocks(blocks: list[DatedBlock], plan_start_date: date) -> list[FixedBlock]:
    """Convenience: convert a whole list at once. Blocks dated before the plan's start are
    skipped rather than raising, same reasoning as extracted_tasks_to_dynamic_tasks.
    """
    result = []
    for block in blocks:
        try:
            result.append(dated_block_to_fixed_block(block, plan_start_date))
        except ValueError:
            continue
    return result