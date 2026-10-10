"""Everything one plan needs, read from the student's saved rows: the window from now to the
last deadline in play, fixed blocks, open tasks with their plan cuts, sleep, and warnings."""
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from scheduler.commutes import commute_overlaps, expand_commutes, overlap_warnings
from scheduler.calendar_utils import expand_fixed_blocks, extracted_task_to_dynamic_task, find_overlaps
from scheduler.db import (
    COMMUTES, DATED_BLOCKS, EXTRACTED_TASKS, PLANNER_TABLES, WEEKLY_PATTERNS, Unreadable, get_plan_cuts,
    load_settings,
)
from scheduler.models import (
    DynamicTask, ExtractedTask, FixedBlock, PlanAnchor, ProfileSettings, ScheduleWarning, SleepRule,
)
from scheduler.nights import sleep_setup
from scheduler.units import SLOTS_PER_DAY, clock_range, format_hours, next_slot

MAX_OVERLAPS_SHOWN = 5


@dataclass
class FitInputs:
    anchor: PlanAnchor
    fixed: list[FixedBlock]
    planned: list[tuple[int, DynamicTask]]  # (saved task id, task with its plan cut applied)
    new_task: DynamicTask | None
    sleep_rules: list[SleepRule]
    settings: ProfileSettings
    warnings: list[ScheduleWarning] = field(default_factory=list)


def _planned_and_overdue(saved_tasks: list[tuple[int, ExtractedTask]], cuts: dict[int, int],
                         start: date, now: datetime, session: int):
    """One pass over the saved open tasks: ([(saved task id, DynamicTask)] with plan cuts
    applied, warnings). A task whose due time is at or before `now` is left out of the plan with
    a hard task_overdue warning; a fully cut task gets a hard task_dropped warning, a partly cut
    one a soft task_cut note. Saved tasks are never changed."""
    planned, warnings = [], []
    for task_id, saved in saved_tasks:
        if saved.completed_at:
            continue
        if saved.due_at() <= now:
            warnings.append(ScheduleWarning.hard("task_overdue", (
                f"'{saved.title}' was due {saved.due_label()} and isn't closed. "
                "Mark it done or missed in manage_tasks.py ([f]).")))
            continue
        task = extracted_task_to_dynamic_task(saved, start, session)
        remaining = task.duration_slots - cuts.get(task_id, 0)
        if remaining <= 0:  # every saved task has a due date, so a full drop misses it: never silent
            warnings.append(ScheduleWarning.hard("task_dropped", (
                f"'{saved.title}' is left out of the plan (all its time was cut to make room) and won't be "
                f"done by {saved.due_label()}. It gets time back when you finish another task.")))
            continue
        if remaining < task.duration_slots:
            warnings.append(ScheduleWarning.soft("task_cut", (
                f"'{saved.title}' is planned at {format_hours(remaining)} of its "
                f"{format_hours(task.duration_slots)} (cut to make room for another task).")))
        planned.append((task_id, task.model_copy(update={"duration_slots": remaining, "saved_id": task_id})))
    return planned, warnings


def planned_tasks(conn, student_id: int, anchor: PlanAnchor):
    """[(saved task id, DynamicTask)] with plan cuts applied, as at the start of the anchor's
    first day. Saved tasks are never changed."""
    start = datetime.combine(anchor.start_date, datetime.min.time())
    session = load_settings(conn, student_id).default_max_session_slots
    return _planned_and_overdue(EXTRACTED_TASKS.get(conn, student_id), get_plan_cuts(conn, student_id),
                                anchor.start_date, start, session)[0]


def unreadable_warnings(unreadable: list[Unreadable]) -> list[ScheduleWarning]:
    """Hard warnings for saved rows that no longer pass their checks; they are left out of the plan."""
    return [ScheduleWarning.hard("saved_row_unreadable", (
                f"A saved {u.table.label} (id {u.row_id}) can't be read ({u.reason}) and is left out of the "
                "plan. Delete it in manage_tasks.py ([u]) and add it again."))
            for u in unreadable if not u.closed]  # a closed task is history


def overlap_lines(start_date: date, overlaps: list[tuple[FixedBlock, FixedBlock]]) -> list[str]:
    """'Mon 05 Oct: 'A' 10:00-12:00 overlaps 'B' 11:00-13:00', the first few pairs, then '...and N more'."""
    lines = []
    for a, b in overlaps[:MAX_OVERLAPS_SHOWN]:
        d = start_date + timedelta(days=a.day)
        lines.append(f"{d:%a %d %b}: '{a.title}' {clock_range(a.start_slot, a.end_slot)} "
                     f"overlaps '{b.title}' {clock_range(b.start_slot, b.end_slot)}")
    if len(overlaps) > MAX_OVERLAPS_SHOWN:
        lines.append(f"...and {len(overlaps) - MAX_OVERLAPS_SHOWN} more overlap(s)")
    return lines


def _block_overlap_warnings(anchor: PlanAnchor, overlaps) -> list[ScheduleWarning]:
    """Saved classes/sessions that overlap are a hard warning, not an error: both are kept and the
    solver plans around their union (merge_fixed_spans), so one bad import never blocks planning."""
    return [ScheduleWarning.hard("block_overlap", (
                f"{line} (both kept; tasks avoid both). If one is wrong, re-run import_schedule.py to replace it."))
            for line in overlap_lines(anchor.start_date, overlaps)]


def starts_from(task: DynamicTask, now: datetime) -> DynamicTask:
    """The task, never planned before the first slot that hasn't started yet."""
    day, slot = divmod(next_slot(now), SLOTS_PER_DAY)
    return task.model_copy(update={"earliest_start_day": day, "earliest_start_slot": slot})


def build_fit_inputs(conn, student_id: int, now: datetime,
                     new_task: ExtractedTask | None = None, min_days: int = 1) -> FitInputs:
    """Everything the solver needs, from `now` until the last deadline in play. Nothing is placed
    in the past, weekly classes are expanded across the whole window, and the window reaches the
    LATEST deadline (not just the new task's) so later-deadline tasks have room to move."""
    settings = load_settings(conn, student_id)
    today, first_slot = now.date(), next_slot(now)
    session = settings.default_max_session_slots

    # each planner table is read once: its readable rows are planned, its unreadable ones warned about
    reads = {table: table.read(conn, student_id) for table in PLANNER_TABLES}
    saved = {table: [item for _, item in readable] for table, (readable, _) in reads.items()}
    unreadable = [u for _, bad in reads.values() for u in bad]

    open_tasks, overdue_warnings = _planned_and_overdue(reads[EXTRACTED_TASKS][0], get_plan_cuts(conn, student_id),
                                                        today, now, session)
    planned = [(i, starts_from(t, now)) for i, t in open_tasks]
    new_dyn = starts_from(extracted_task_to_dynamic_task(new_task, today, session), now) if new_task else None

    tasks = [t for _, t in planned] + ([new_dyn] if new_dyn else [])
    last_deadline = max((t.deadline_day for t in tasks if t.deadline_day is not None), default=0)
    num_days = max(min(last_deadline + 1, settings.plan_horizon_max_days), min_days)
    # every task's start range must fit inside the window even when it is too late to finish
    num_days = max([num_days] + [math.ceil((first_slot + t.duration_slots) / SLOTS_PER_DAY) for t in tasks])
    anchor = PlanAnchor(start_date=today, num_days=num_days)

    # expand once, for the window plus the morning after (only looked at, for the wake-up)
    look = PlanAnchor(start_date=today, num_days=num_days + 1)
    classes = expand_fixed_blocks(saved[WEEKLY_PATTERNS], saved[DATED_BLOCKS], look)
    commute_blocks = expand_commutes(saved[COMMUTES], look)
    morning_after = [b for b in classes + commute_blocks if b.day == num_days]

    def ahead(blocks):  # inside the window and not already over
        return [b for b in blocks if b.day < num_days and not (b.day == 0 and b.end_slot <= first_slot)]

    fixed, commutes = ahead(classes), ahead(commute_blocks)
    # overlapping commutes are never rejected, only noted
    warnings = (_block_overlap_warnings(anchor, find_overlaps(fixed))
                + overlap_warnings(commute_overlaps(fixed, commutes), anchor))
    fixed, sleep_rules = sleep_setup(fixed + commutes, morning_after, num_days, settings, now)
    warnings += overdue_warnings + unreadable_warnings(unreadable)
    return FitInputs(anchor, fixed, planned, new_dyn, sleep_rules, settings, warnings)
