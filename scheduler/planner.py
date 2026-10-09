from datetime import date, datetime, timedelta   
from scheduler.fit_check import (
    build_fit_inputs, next_morning_wake, overlap_error, planned_tasks, task_date_warnings, with_commutes,
    with_latest_wake,
)
from scheduler.calendar_utils import build_plan_inputs, find_overlaps
from scheduler.db import (
    get_dated_blocks, get_extracted_tasks, get_weekly_patterns, load_settings,
)   
from scheduler.models import FixedBlock, PlanAnchor, SLOTS_PER_DAY, slot_to_time
from scheduler.solver import build_schedule, sleep_warnings, task_warnings


def plan_from_saved(conn, student_id: int, num_days: int | None = None, start_date: date | None = None,
                    time_limit_seconds: float = 30.0, now: datetime | None = None):   
    """Default (no start_date): a continuous plan from `now` to the last deadline; num_days is only
    a minimum. start_date given: the old explicit window of num_days (default 7) from that date."""

    #gets all the patterns, dated_blocks, saved_tasks for a specicic student from the database and raies a value error if none exist
    patterns = [p for _, p in get_weekly_patterns(conn, student_id)]
    dated = [d for _, d in get_dated_blocks(conn, student_id)]
    saved_tasks = [t for _, t in get_extracted_tasks(conn, student_id)]
    if not (patterns or dated or saved_tasks):
        raise ValueError("no saved schedule items -- run import_schedule.py first")
    
    #fit stores the necessary constraints of the schedule
    #builds the schedule, storing both the schduled and unscheduled tasks
    #then returns the anchor, fixed blocks, items and all warnings
    if start_date is None:  #   -- continuous mode
        fit = build_fit_inputs(conn, student_id, now or datetime.now(), min_days=num_days or 1)
        items, unscheduled = build_schedule(
            fit.fixed, [t for _, t in fit.planned], num_days=fit.anchor.num_days,
            sleep_rules=fit.sleep_rules, time_limit_seconds=time_limit_seconds, settings=fit.settings,
        )
        return fit.anchor, fit.fixed, items, sleep_warnings(fit.sleep_rules, items) + task_warnings(unscheduled) + fit.warnings   

    #after initializing muliple attributes, then ensures no overlaps occur in fixed blocks
    #stores commute warnings, settings, sleep rules, waking rules
    #ensures that there is a fixed block accounting for the rollover of the student's sleep time from the previous day
    #proceeds to build the full schedule, returning the anchor, fixed blocks, items and all warnings
    num_days = num_days or 7
    anchor = PlanAnchor(start_date=start_date, num_days=num_days)
    fixed, _ = build_plan_inputs(patterns, dated, saved_tasks, anchor)
    tasks = [t for _, t in planned_tasks(conn, student_id, anchor)]  # honours plan cuts
    overlaps = find_overlaps(fixed)
    if overlaps:
        raise ValueError(overlap_error(anchor, overlaps))   
    
    fixed, commute_warnings = with_commutes(conn, student_id, fixed, anchor)
    settings = load_settings(conn, student_id)
    sleep_rules = [settings.default_sleep_rule(night=n) for n in range(num_days)]
    rule0 = sleep_rules[0]
    wake = rule0.preferred_bed + rule0.length_slots - SLOTS_PER_DAY
    wake = min([wake] + [b.start_slot for b in fixed if b.day == 0])
    if wake > 0:
        fixed = fixed + [FixedBlock(title="Sleep (night before)", day=0, start_slot=0, end_slot=wake)]

    sleep_rules = with_latest_wake(sleep_rules, next_morning_wake(conn, student_id, anchor, settings))

    items, unscheduled = build_schedule(
        fixed, tasks, num_days=num_days, sleep_rules=sleep_rules,
        time_limit_seconds=time_limit_seconds, settings=settings,
    )
    #compiles all warnings
    warnings = (sleep_warnings(sleep_rules, items) + task_warnings(unscheduled) + commute_warnings
                + task_date_warnings(conn, student_id, (now or datetime.now()).date()))
    return anchor, fixed, items, warnings

def format_plan(anchor: PlanAnchor, fixed, items, warnings) -> list[str]:   
    """Plain-text lines: fixed blocks and solved items merged by real date and time."""
    rows = [(b.day, b.start_slot, b.end_slot, "fixed", b.title) for b in fixed]
    rows += [(i.day, i.start_slot, i.end_slot, i.kind, i.title) for i in items if i.kind != "fixed"]
    rows.sort()
    lines, last_day = [], None
    for day, start, end, kind, title in rows:
        if day != last_day:
            d = anchor.start_date + timedelta(days=day)
            lines.append(f"{d:%a %d %b %Y}")
            last_day = day
        lines.append(f"  {slot_to_time(start)}-{slot_to_time(end % SLOTS_PER_DAY)}  [{kind}]  {title}")
    if warnings:
        lines.append("Warnings:")
        lines += [f"  [{w.severity}] {w.kind}: {w.message}" for w in warnings]
    return lines