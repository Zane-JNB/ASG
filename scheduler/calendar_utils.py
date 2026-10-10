"""Stored, calendar-dated items -> the solver's day-indexed inputs for one plan window."""
from datetime import date

from scheduler.models import DatedBlock, DynamicTask, ExtractedTask, FixedBlock, PlanAnchor, WeeklyPattern
from scheduler.units import time_to_slot, weekday_name


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


def extracted_task_to_dynamic_task(task: ExtractedTask, plan_start_date: date,
                                   max_session_slots: int) -> DynamicTask:
    """One saved task (real calendar date) as a DynamicTask (relative day index)."""
    return DynamicTask(
        title=task.title,
        duration_slots=task.duration_slots,
        priority=task.priority,
        difficulty=task.difficulty,
        splittable=task.splittable,
        may_cut_sleep=task.may_cut_sleep,
        max_session_slots=max_session_slots,
        deadline_day=day_index_for_date(plan_start_date, date.fromisoformat(task.date)),
        deadline_slot=task.due_slot(),  # end of day unless the task has a due time
    )


def _block(item: WeeklyPattern | DatedBlock, day: int) -> FixedBlock:
    return FixedBlock(title=item.title, start_slot=time_to_slot(item.start_time),
                      end_slot=time_to_slot(item.end_time), day=day)


def expand_fixed_blocks(patterns: list[WeeklyPattern], dated_blocks: list[DatedBlock],
                        anchor: PlanAnchor) -> list[FixedBlock]:
    """Saved classes and dated sessions as FixedBlocks for THIS anchor window: each weekly
    pattern on every matching day, each dated block on its own day. Dates outside the window
    are left out (a document may list sessions that are already over)."""
    day_of = {d.isoformat(): i for i, d in enumerate(anchor.dates)}
    return ([_block(p, i) for p in patterns for i, d in enumerate(anchor.dates) if weekday_name(d) == p.day]
            + [_block(b, day_of[b.date]) for b in dated_blocks if b.date in day_of])


def window_through(today: date, dates: list[str | None]) -> PlanAnchor:
    """A window from today through the last of dates (None and past dates ignored), and at least
    a full week, so every weekly class and commute is in it once. For overlap checks only."""
    last = max([today] + [date.fromisoformat(d) for d in dates if d])
    return PlanAnchor(start_date=today, num_days=max((last - today).days + 1, 7))


def find_overlaps(blocks: list[FixedBlock]) -> list[tuple[FixedBlock, FixedBlock]]:
    """Pairs of fixed blocks whose time ranges overlap. Back-to-back (end == next start) is fine."""
    ordered = sorted(blocks, key=lambda b: b.span)
    pairs = []
    for i, b1 in enumerate(ordered):
        for b2 in ordered[i + 1:]:
            if b2.span[0] >= b1.span[1]:  # sorted by start, so nothing later can overlap b1
                break
            pairs.append((b1, b2))
    return pairs
