import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from scheduler.commutes import commute_overlaps, expand_commutes, overlap_warnings
from scheduler.calendar_utils import build_plan_inputs, extracted_task_to_dynamic_task, find_overlaps
from scheduler.db import get_commutes, get_dated_blocks, get_weekly_patterns, load_settings, get_plan_cuts, get_extracted_tasks
from scheduler.models import (
    DynamicTask, ExtractedTask, FixedBlock, MINUTES_PER_SLOT, PlanAnchor, ProfileSettings,
    SLOTS_PER_DAY, ScheduleWarning, SleepRule, slot_to_time,)

def planned_tasks(conn, student_id: int, anchor: PlanAnchor):   
    """[(saved task id, DynamicTask)] with plan cuts applied. Saved tasks are never changed."""
    cuts = get_plan_cuts(conn, student_id)
    session = load_settings(conn, student_id).default_max_session_slots
     
    result = []
    for task_id, saved in get_extracted_tasks(conn, student_id):
        if saved.completed_at:
            continue   
        try:
            task = extracted_task_to_dynamic_task(saved, anchor.start_date, session)
        except ValueError:  # due before the plan starts
            continue
        remaining = task.duration_slots - cuts.get(task_id, 0)
        if remaining > 0:
            result.append((task_id, task.model_copy(update={"duration_slots": remaining, "saved_id": task_id})))
    return result

def overlap_error(anchor: PlanAnchor, overlaps) -> str:    
    lines = []
    for a, b in overlaps[:5]:
        d = anchor.start_date + timedelta(days=a.day)
        lines.append(f"  {d:%a %d %b}: '{a.title}' {slot_to_time(a.start_slot)}-"
                     f"{slot_to_time(a.end_slot % SLOTS_PER_DAY)} overlaps '{b.title}' "
                     f"{slot_to_time(b.start_slot)}-{slot_to_time(b.end_slot % SLOTS_PER_DAY)}")
    more = f"\n  ...and {len(overlaps) - 5} more" if len(overlaps) > 5 else ""
    return ("Some saved blocks overlap, so no schedule is possible:\n" + "\n".join(lines) + more
            + "\nRe-run import_schedule.py and delete or fix the duplicates.")

@dataclass
class FitInputs: 
    anchor: PlanAnchor #stores all relevant dates, start_date, num_dates
    fixed: list[FixedBlock]
    #planned: (saved task id, task) which also ensure that when space is freed up for a task that was cut, it is properly reallocated
    planned: list[tuple[int, DynamicTask]] 
    new_task: DynamicTask | None 
    sleep_rules: list[SleepRule]
    settings: ProfileSettings
    warnings: list[ScheduleWarning] = field(default_factory=list)

def with_commutes(conn, student_id: int, fixed: list[FixedBlock], anchor: PlanAnchor,
                  after_slot: int = 0):  
    """(fixed + commute blocks, soft overlap warnings). Commutes are never rejected for overlapping."""
    commutes = [c for _, c in get_commutes(conn, student_id)]
    blocks = [b for b in expand_commutes(commutes, anchor)
              if not (b.day == 0 and b.end_slot <= after_slot)]  # skip commutes already over
    return fixed + blocks, overlap_warnings(commute_overlaps(fixed, blocks), anchor)
    
def next_slot(now: datetime) -> int:   
    """First 15-minute slot that has not started yet, counted from midnight today (96 = tomorrow 00:00)."""
    seconds = now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6
    return math.ceil(seconds / (MINUTES_PER_SLOT * 60))

#ensures the earliest start ate for a task is not before or after today.
def starts_from(task: DynamicTask, now: datetime) -> DynamicTask:
    day, slot = divmod(next_slot(now), SLOTS_PER_DAY)
    return task.model_copy(update={"earliest_start_day": day, "earliest_start_slot": slot})

def build_fit_inputs(conn, student_id: int, now: datetime,
                     new_task: ExtractedTask | None = None, min_days: int = 1) -> FitInputs:   
    """Everything the solver needs, from `now` until the last deadline in play. Nothing is placed
    in the past, weekly classes are expanded across the whole window, and the window reaches the
    LATEST deadline (not just the new task's) so later-deadline tasks have room to move."""
    settings = load_settings(conn, student_id)
    today = now.date() #makes day 0 = today
    day0 = PlanAnchor(start_date=today, num_days=1) #anchor's plan with today's date.
    start_day, start_slot = divmod(next_slot(now), SLOTS_PER_DAY)

    def from_now(t: DynamicTask) -> DynamicTask:
        return starts_from(t, now)

    planned = [(i, from_now(t)) for i, t in planned_tasks(conn, student_id, day0)]
    new_dyn = from_now(extracted_task_to_dynamic_task(new_task, today, settings.default_max_session_slots)) if new_task else None

    tasks = [t for _, t in planned] + ([new_dyn] if new_dyn else [])
    last_deadline = max((t.deadline_day for t in tasks if t.deadline_day is not None), default=0)
    num_days = max(min(last_deadline + 1, settings.plan_horizon_max_days), min_days)   
    # every task's start range must fit inside the window even when it is too late to finish
    num_days = max([num_days] + [math.ceil((next_slot(now) + t.duration_slots) / SLOTS_PER_DAY) for t in tasks])
    anchor = PlanAnchor(start_date=today, num_days=num_days)

    patterns = [p for _, p in get_weekly_patterns(conn, student_id)]
    dated = [b for _, b in get_dated_blocks(conn, student_id)]
    fixed, _ = build_plan_inputs(patterns, dated, [], anchor)
    overlaps = find_overlaps(fixed)
    if overlaps:
        raise ValueError(overlap_error(anchor, overlaps))
    fixed = [b for b in fixed if not (b.day == 0 and b.end_slot <= next_slot(now))]
    fixed, commute_warnings = with_commutes(conn, student_id, fixed, anchor, next_slot(now))

    sleep_rules = [settings.default_sleep_rule(night=n) for n in range(num_days)]
    rule0 = sleep_rules[0]
    wake = rule0.preferred_bed + rule0.length_slots - SLOTS_PER_DAY
    wake = min([wake] + [b.start_slot for b in fixed if b.day == 0])
    if start_day == 0 and wake > start_slot:  # still morning-sleep hours: keep them free
        fixed = fixed + [FixedBlock(title="Sleep (night before)", day=0, start_slot=0, end_slot=wake)]
    # tonight's bedtime cannot be earlier than now
    bed = max(rule0.earliest_bed, next_slot(now))
    sleep_rules[0] = rule0.model_copy(update={"earliest_bed": bed,
                                              "preferred_bed": max(rule0.preferred_bed, bed),
                                              "latest_bed": max(rule0.latest_bed, bed)})
    return FitInputs(anchor, fixed, planned, new_dyn, sleep_rules, settings, commute_warnings)  