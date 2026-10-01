import math
from dataclasses import dataclass
from datetime import datetime, timedelta, date

from scheduler.calendar_utils import build_plan_inputs, extracted_task_to_dynamic_task, find_overlaps
from scheduler.db import get_dated_blocks, get_weekly_patterns, load_settings, get_plan_cuts, get_extracted_tasks
from scheduler.models import (
    DynamicTask, ExtractedTask, FixedBlock, MINUTES_PER_SLOT, PlanAnchor, ProfileSettings,
    SLOTS_PER_DAY, SleepRule, slot_to_time,
)

def planned_tasks(conn, student_id: int, anchor: PlanAnchor):  # NEW
    """[(saved task id, DynamicTask)] with plan cuts applied. Saved tasks are never changed."""
    cuts = get_plan_cuts(conn, student_id)
    session = load_settings(conn, student_id).default_max_session_slots
     # NEW
    result = []
    for task_id, saved in get_extracted_tasks(conn, student_id):
        if saved.completed_at:
            continue  # NEW
        try:
            task = extracted_task_to_dynamic_task(saved, anchor.start_date, session)
        except ValueError:  # due before the plan starts
            continue
        remaining = task.duration_slots - cuts.get(task_id, 0)
        if remaining > 0:
            result.append((task_id, task.model_copy(update={"duration_slots": remaining})))
    return result

def overlap_error(anchor: PlanAnchor, overlaps) -> str:  # NEW (moved here from plan_from_saved)
    lines = []
    for a, b in overlaps[:5]:
        d = anchor.start_date + timedelta(days=a.day)
        lines.append(f"  {d:%a %d %b}: '{a.title}' {slot_to_time(a.start_slot)}-"
                     f"{slot_to_time(a.end_slot % SLOTS_PER_DAY)} overlaps '{b.title}' "
                     f"{slot_to_time(b.start_slot)}-{slot_to_time(b.end_slot % SLOTS_PER_DAY)}")
    more = f"\n  ...and {len(overlaps) - 5} more" if len(overlaps) > 5 else ""
    return ("Some saved blocks overlap, so no schedule is possible:\n" + "\n".join(lines) + more
            + "\nRe-run import_schedule.py and delete or fix the duplicates.")

# def plan_from_saved(conn, student_id: int, num_days: int = 7, start_date: date | None = None,
#                     time_limit_seconds: float = 30.0):  # NEW
#     """Load the student's saved extracted items, anchor them to `start_date`, and solve.
#     start_date defaults to TOMORROW: day 0 starts at 00:00, so planning from today could
#     put tasks in hours that have already passed.
#     Returns (anchor, fixed_blocks, scheduled_items, warnings)."""
#     patterns = [p for _, p in get_weekly_patterns(conn, student_id)]
#     dated = [b for _, b in get_dated_blocks(conn, student_id)]
#     saved_tasks = [t for _, t in get_extracted_tasks(conn, student_id)]
#     if not (patterns or dated or saved_tasks):
#         raise ValueError("no saved schedule items -- run import_schedule.py first")

#     anchor = PlanAnchor(start_date=start_date or date.today() + timedelta(days=1),
#                         num_days=num_days)
#     fixed, _ = build_plan_inputs(patterns, dated, saved_tasks, anchor)  # NEW
#     tasks = [t for _, t in planned_tasks(conn, student_id, anchor)]  # NEW -- honours plan cuts
#     overlaps = find_overlaps(fixed)  # NEW -- the solver only says "no valid schedule"
#     if overlaps:
#         lines = []
#         for a, b in overlaps[:5]:
#             d = anchor.start_date + timedelta(days=a.day)
#             lines.append(f"  {d:%a %d %b}: '{a.title}' {slot_to_time(a.start_slot)}-"
#                          f"{slot_to_time(a.end_slot % SLOTS_PER_DAY)} overlaps '{b.title}' "
#                          f"{slot_to_time(b.start_slot)}-{slot_to_time(b.end_slot % SLOTS_PER_DAY)}")
#         more = f"\n  ...and {len(overlaps) - 5} more" if len(overlaps) > 5 else ""
#         raise ValueError("Some saved blocks overlap, so no schedule is possible:\n"
#                          + "\n".join(lines) + more
#                          + "\nRe-run import_schedule.py and delete or fix the duplicates.")
#     settings = load_settings(conn, student_id)
#     sleep_rules = [settings.default_sleep_rule(night=n) for n in range(num_days)]
#     rule0 = sleep_rules[0]
#     wake = rule0.preferred_bed + rule0.length_slots - SLOTS_PER_DAY
#     wake = min([wake] + [b.start_slot for b in fixed if b.day == 0])
#     if wake > 0:
#         fixed = fixed + [FixedBlock(title="Sleep (night before)", day=0, start_slot=0, end_slot=wake)]
#     items, unscheduled = build_schedule(
#         fixed, tasks, num_days=num_days, sleep_rules=sleep_rules,
#         time_limit_seconds=time_limit_seconds, settings=settings,
#     )
#     warnings = sleep_warnings(sleep_rules, items) + task_warnings(unscheduled)
#     return anchor, fixed, items, warnings



@dataclass
class FitInputs:  # NEW
    anchor: PlanAnchor
    fixed: list[FixedBlock]
    planned: list[tuple[int, DynamicTask]]  # (saved task id, task) with plan cuts applied
    new_task: DynamicTask | None
    sleep_rules: list[SleepRule]
    settings: ProfileSettings


def next_slot(now: datetime) -> int:  # NEW
    """First 15-minute slot that has not started yet, counted from midnight today (96 = tomorrow 00:00)."""
    seconds = now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6
    return math.ceil(seconds / (MINUTES_PER_SLOT * 60))

def starts_from(task: DynamicTask, now: datetime) -> DynamicTask:
    day, slot = divmod(next_slot(now), SLOTS_PER_DAY)
    return task.model_copy(update={"earliest_start_day": day, "earliest_start_slot": slot})

def build_fit_inputs(conn, student_id: int, now: datetime,
                     new_task: ExtractedTask | None = None, min_days: int = 1) -> FitInputs:  # NEW min_days
    """Everything the solver needs, from `now` until the last deadline in play. Nothing is placed
    in the past, weekly classes are expanded across the whole window, and the window reaches the
    LATEST deadline (not just the new task's) so later-deadline tasks have room to move."""
    settings = load_settings(conn, student_id)
    today = now.date()
    day0 = PlanAnchor(start_date=today, num_days=1)
    start_day, start_slot = divmod(next_slot(now), SLOTS_PER_DAY)

    def from_now(t: DynamicTask) -> DynamicTask:
        return starts_from(t, now)

    planned = [(i, from_now(t)) for i, t in planned_tasks(conn, student_id, day0)]
    new_dyn = from_now(extracted_task_to_dynamic_task(new_task, today, settings.default_max_session_slots)) if new_task else None

    tasks = [t for _, t in planned] + ([new_dyn] if new_dyn else [])
    last_deadline = max((t.deadline_day for t in tasks if t.deadline_day is not None), default=0)
    num_days = max(min(last_deadline + 1, settings.plan_horizon_max_days), min_days)  # NEW min_days
    # every task's start range must fit inside the window even when it is too late to finish
    num_days = max([num_days] + [math.ceil((next_slot(now) + t.duration_slots) / SLOTS_PER_DAY) for t in tasks])
    anchor = PlanAnchor(start_date=today, num_days=num_days)

    patterns = [p for _, p in get_weekly_patterns(conn, student_id)]
    dated = [b for _, b in get_dated_blocks(conn, student_id)]
    fixed, _ = build_plan_inputs(patterns, dated, [], anchor)
    overlaps = find_overlaps(fixed)
    if overlaps:
        raise ValueError(overlap_error(anchor, overlaps))
    fixed = [b for b in fixed if not (b.day == 0 and b.end_slot <= next_slot(now))]  # NEW  already over

    sleep_rules = [settings.default_sleep_rule(night=n) for n in range(num_days)]
    rule0 = sleep_rules[0]
    wake = rule0.preferred_bed + rule0.length_slots - SLOTS_PER_DAY
    wake = min([wake] + [b.start_slot for b in fixed if b.day == 0])
    if start_day == 0 and wake > start_slot:  # still morning-sleep hours: keep them free
        fixed = fixed + [FixedBlock(title="Sleep (night before)", day=0, start_slot=0, end_slot=wake)]
    return FitInputs(anchor, fixed, planned, new_dyn, sleep_rules, settings)