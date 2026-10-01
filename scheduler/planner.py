from datetime import date, datetime, timedelta  # NEW datetime

from scheduler.calendar_utils import build_plan_inputs, find_overlaps
from scheduler.db import (
    get_dated_blocks, get_extracted_tasks, get_weekly_patterns, load_settings,
)
from scheduler.fit_check import build_fit_inputs, overlap_error, planned_tasks  # NEW
from scheduler.models import FixedBlock, PlanAnchor, SLOTS_PER_DAY, slot_to_time
from scheduler.solver import build_schedule, sleep_warnings, task_warnings


def plan_from_saved(conn, student_id: int, num_days: int | None = None, start_date: date | None = None,
                    time_limit_seconds: float = 30.0, now: datetime | None = None):  # NEW  now, num_days optional
    """Default (no start_date): a continuous plan from `now` to the last deadline; num_days is only
    a minimum. start_date given: the old explicit window of num_days (default 7) from that date."""
    patterns = [p for _, p in get_weekly_patterns(conn, student_id)]
    dated = [b for _, b in get_dated_blocks(conn, student_id)]
    saved_tasks = [t for _, t in get_extracted_tasks(conn, student_id)]
    if not (patterns or dated or saved_tasks):
        raise ValueError("no saved schedule items -- run import_schedule.py first")

    if start_date is None:  # NEW -- continuous mode
        fit = build_fit_inputs(conn, student_id, now or datetime.now(), min_days=num_days or 1)
        items, unscheduled = build_schedule(
            fit.fixed, [t for _, t in fit.planned], num_days=fit.anchor.num_days,
            sleep_rules=fit.sleep_rules, time_limit_seconds=time_limit_seconds, settings=fit.settings,
        )
        return fit.anchor, fit.fixed, items, sleep_warnings(fit.sleep_rules, items) + task_warnings(unscheduled)

    num_days = num_days or 7
    anchor = PlanAnchor(start_date=start_date, num_days=num_days)
    fixed, _ = build_plan_inputs(patterns, dated, saved_tasks, anchor)
    tasks = [t for _, t in planned_tasks(conn, student_id, anchor)]  # honours plan cuts
    overlaps = find_overlaps(fixed)
    if overlaps:
        raise ValueError(overlap_error(anchor, overlaps))  # NEW  shared message
    settings = load_settings(conn, student_id)
    sleep_rules = [settings.default_sleep_rule(night=n) for n in range(num_days)]
    rule0 = sleep_rules[0]
    wake = rule0.preferred_bed + rule0.length_slots - SLOTS_PER_DAY
    wake = min([wake] + [b.start_slot for b in fixed if b.day == 0])
    if wake > 0:
        fixed = fixed + [FixedBlock(title="Sleep (night before)", day=0, start_slot=0, end_slot=wake)]
    items, unscheduled = build_schedule(
        fixed, tasks, num_days=num_days, sleep_rules=sleep_rules,
        time_limit_seconds=time_limit_seconds, settings=settings,
    )
    warnings = sleep_warnings(sleep_rules, items) + task_warnings(unscheduled)
    return anchor, fixed, items, warnings

def format_plan(anchor: PlanAnchor, fixed, items, warnings) -> list[str]:  # NEW
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