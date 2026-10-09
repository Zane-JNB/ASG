import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from scheduler.commutes import commute_overlaps, expand_commutes, overlap_warnings
from scheduler.calendar_utils import build_plan_inputs, extracted_task_to_dynamic_task, find_overlaps
from scheduler.db import (
    get_commutes, get_dated_blocks, get_extracted_tasks, get_plan_cuts, get_unreadable_items, get_weekly_patterns,
    load_settings,
)
from scheduler.drop_review import _h
from scheduler.models import (
    DynamicTask, ExtractedTask, FixedBlock, MINUTES_PER_SLOT, PlanAnchor, ProfileSettings,
    SLOTS_PER_DAY, ScheduleWarning, SleepRule, slot_to_time,)

def _planned_and_overdue(conn, student_id: int, anchor: PlanAnchor, now: datetime, session: int):
    """One pass over the saved open tasks: ([(saved task id, DynamicTask)] with plan cuts
    applied, warnings). A task whose due time is at or before `now` is left out of the plan with
    a hard task_overdue warning; a fully cut task gets a hard task_dropped warning, a partly cut
    one a soft task_cut note. Saved tasks are never changed."""
    cuts = get_plan_cuts(conn, student_id)
    planned, warnings = [], []
    for task_id, saved in get_extracted_tasks(conn, student_id):
        if saved.completed_at:
            continue
        if saved.due_at() <= now:
            warnings.append(ScheduleWarning(
                severity="hard", kind="task_overdue",
                message=f"'{saved.title}' was due {saved.due_label()} and isn't closed. Mark it done or missed in manage_tasks.py ([f])."))
            continue
        task = extracted_task_to_dynamic_task(saved, anchor.start_date, session)
        remaining = task.duration_slots - cuts.get(task_id, 0)
        if remaining <= 0:  # every saved task has a due date, so a full drop misses it: never silent
            warnings.append(ScheduleWarning(
                severity="hard", kind="task_dropped",
                message=f"'{saved.title}' is left out of the plan (all its time was cut to make room) "
                        f"and won't be done by {saved.due_label()}. It gets time back when you finish another task."))
            continue
        if remaining < task.duration_slots:
            warnings.append(ScheduleWarning(
                severity="soft", kind="task_cut",
                message=f"'{saved.title}' is planned at {_h(remaining)} of its {_h(task.duration_slots)} "
                        "(cut to make room for another task)."))
        planned.append((task_id, task.model_copy(update={"duration_slots": remaining, "saved_id": task_id})))
    return planned, warnings

def planned_tasks(conn, student_id: int, anchor: PlanAnchor):
    """[(saved task id, DynamicTask)] with plan cuts applied, as at the start of the anchor's
    first day. Saved tasks are never changed."""
    start = datetime.combine(anchor.start_date, datetime.min.time())
    session = load_settings(conn, student_id).default_max_session_slots
    return _planned_and_overdue(conn, student_id, anchor, start, session)[0]

def unreadable_warnings(conn, student_id: int) -> list[ScheduleWarning]:
    """Hard warnings for saved rows that no longer pass their checks; they are left out of the plan."""
    return [ScheduleWarning(
                severity="hard", kind="saved_row_unreadable",
                message=f"A saved {label} (id {row_id}) can't be read ({reason}) and is left out of the plan. "
                        "Delete it in manage_tasks.py ([u]) and add it again.")
            for label, row_id, reason in get_unreadable_items(conn, student_id, include_completed=False)]

def wake_before(day_blocks: list[FixedBlock], settings: ProfileSettings) -> tuple[int, str] | None:
    """(latest wake-up within that day, why) from the day's first class or commute minus the
    wake buffer, or None if nothing is on that day. The one wake-up rule, for every morning."""
    if not day_blocks:
        return None
    first = min(day_blocks, key=lambda b: b.start_slot)
    reason = f"'{first.title}' at {slot_to_time(first.start_slot)} the next day"
    if settings.wake_buffer_slots:
        reason += f" (minus your {settings.wake_buffer_slots * MINUTES_PER_SLOT} min wake-up buffer)"
    return first.start_slot - settings.wake_buffer_slots, reason

def with_wake_limit(rule: SleepRule, next_day: list[FixedBlock], settings: ProfileSettings) -> SleepRule:
    """Cap one night's sleep at the next day's wake-up (never before its earliest bedtime), and
    say why. Unchanged if nothing is on that day."""
    limit = wake_before(next_day, settings)
    if limit is None:
        return rule
    wake, reason = limit
    return rule.model_copy(update={"latest_wake": max(wake + SLOTS_PER_DAY, rule.earliest_bed),
                                   "latest_wake_reason": reason})

def sleep_setup(fixed: list[FixedBlock], morning_after: list[FixedBlock], num_days: int,
                settings: ProfileSettings, now: datetime) -> tuple[list[FixedBlock], list[SleepRule]]:
    """(fixed blocks with this morning's sleep added, one sleep rule per night). The one place
    sleep is set up for a plan:
      - this morning: if it is still sleep time, "Sleep (night before)" runs from 00:00 to the
        wake-up (the usual one, or the first class/commute minus the wake buffer), and "Getting
        ready" keeps the rest of the wake buffer free (also when the plan starts inside it);
      - tonight: bedtime is never before now;
      - every night: ends by the next day's first class/commute minus the wake buffer, and says
        so. The last night's next day (the morning after the window) is only looked at.
    The solver keeps the wake buffer after every night's sleep."""
    start_day, start_slot = divmod(next_slot(now), SLOTS_PER_DAY)
    sleep_rules = [settings.default_sleep_rule(night=n) for n in range(num_days)]
    rule0 = sleep_rules[0]
    today = [b for b in fixed if b.day == 0]
    wake = rule0.preferred_bed + rule0.length_slots - SLOTS_PER_DAY
    limit = wake_before(today, settings)
    wake = max(0, min(wake, limit[0]) if limit else wake)
    if start_day == 0:
        added = []
        if wake > start_slot:  # still morning-sleep hours: keep them free
            added.append(FixedBlock(title="Sleep (night before)", day=0, start_slot=0, end_slot=wake))
        # the normal buffer after a block makes up the rest of the wake buffer; never past the
        # day's first block (a class just after midnight leaves no room for it)
        ready_end = min([wake + settings.wake_buffer_slots - settings.buffer_slots] + [b.start_slot for b in today])
        ready_start = max(wake, start_slot)
        if ready_start < ready_end <= SLOTS_PER_DAY:
            added.append(FixedBlock(title="Getting ready", day=0, start_slot=ready_start, end_slot=ready_end))
        fixed = fixed + added
    # tonight's bedtime cannot be earlier than now
    bed = max(rule0.earliest_bed, next_slot(now))
    sleep_rules[0] = rule0.model_copy(update={"earliest_bed": bed,
                                              "preferred_bed": max(rule0.preferred_bed, bed),
                                              "latest_bed": max(rule0.latest_bed, bed)})
    blocks = fixed + morning_after
    return fixed, [with_wake_limit(rule, [b for b in blocks if b.day == rule.night + 1], settings)
                   for rule in sleep_rules]

MAX_OVERLAPS_SHOWN = 5

def overlap_lines(start_date, overlaps) -> list[str]:
    """'Mon 05 Oct: 'A' 10:00-12:00 overlaps 'B' 11:00-13:00', the first few pairs, then '...and N more'."""
    lines = []
    for a, b in overlaps[:MAX_OVERLAPS_SHOWN]:
        d = start_date + timedelta(days=a.day)
        lines.append(f"{d:%a %d %b}: '{a.title}' {slot_to_time(a.start_slot)}-"
                     f"{slot_to_time(a.end_slot % SLOTS_PER_DAY)} overlaps '{b.title}' "
                     f"{slot_to_time(b.start_slot)}-{slot_to_time(b.end_slot % SLOTS_PER_DAY)}")
    if len(overlaps) > MAX_OVERLAPS_SHOWN:
        lines.append(f"...and {len(overlaps) - MAX_OVERLAPS_SHOWN} more overlap(s)")
    return lines

def overlap_warnings_for_blocks(anchor: PlanAnchor, overlaps) -> list[ScheduleWarning]:
    """Saved classes/sessions that overlap are a hard warning, not an error: both are kept and the
    solver plans around their union (merge_fixed_spans), so one bad import never blocks planning."""
    return [ScheduleWarning(severity="hard", kind="block_overlap",
                            message=f"{line} (both kept; tasks avoid both). If one is wrong, "
                                    "re-run import_schedule.py to replace it.")
            for line in overlap_lines(anchor.start_date, overlaps)]

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

def with_commutes(fixed: list[FixedBlock], commute_blocks: list[FixedBlock], anchor: PlanAnchor,
                  after_slot: int = 0):
    """(fixed + commute blocks, soft overlap warnings). Commutes are never rejected for overlapping."""
    blocks = [b for b in commute_blocks
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
    def from_now(t: DynamicTask) -> DynamicTask:
        return starts_from(t, now)

    open_tasks, overdue_warnings = _planned_and_overdue(conn, student_id, day0, now, settings.default_max_session_slots)
    planned = [(i, from_now(t)) for i, t in open_tasks]
    new_dyn = from_now(extracted_task_to_dynamic_task(new_task, today, settings.default_max_session_slots)) if new_task else None

    tasks = [t for _, t in planned] + ([new_dyn] if new_dyn else [])
    last_deadline = max((t.deadline_day for t in tasks if t.deadline_day is not None), default=0)
    num_days = max(min(last_deadline + 1, settings.plan_horizon_max_days), min_days)   
    # every task's start range must fit inside the window even when it is too late to finish
    num_days = max([num_days] + [math.ceil((next_slot(now) + t.duration_slots) / SLOTS_PER_DAY) for t in tasks])
    anchor = PlanAnchor(start_date=today, num_days=num_days)

    # read and expand once, for the window plus the morning after (only looked at, for the wake-up)
    patterns = [p for _, p in get_weekly_patterns(conn, student_id)]
    dated = [b for _, b in get_dated_blocks(conn, student_id)]
    commutes = [c for _, c in get_commutes(conn, student_id)]
    look = PlanAnchor(start_date=today, num_days=num_days + 1)
    classes, _ = build_plan_inputs(patterns, dated, [], look)
    commute_blocks = expand_commutes(commutes, look)
    morning_after = [b for b in classes + commute_blocks if b.day == num_days]
    fixed = [b for b in classes if b.day < num_days]
    fixed = [b for b in fixed if not (b.day == 0 and b.end_slot <= next_slot(now))]
    block_warnings = overlap_warnings_for_blocks(anchor, find_overlaps(fixed))  # only clashes not already over
    fixed, commute_warnings = with_commutes(fixed, [b for b in commute_blocks if b.day < num_days],
                                            anchor, next_slot(now))
    fixed, sleep_rules = sleep_setup(fixed, morning_after, num_days, settings, now)
    warnings = block_warnings + commute_warnings + overdue_warnings + unreadable_warnings(conn, student_id)
    return FitInputs(anchor, fixed, planned, new_dyn, sleep_rules, settings, warnings)  