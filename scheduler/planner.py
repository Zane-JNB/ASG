from datetime import datetime
from scheduler.fit_check import build_fit_inputs
from scheduler.db import has_saved_items
from scheduler.models import PlanAnchor
from scheduler.units import clock_range
from scheduler.solver import sleep_warnings, task_warnings


def plan_from_saved(conn, student_id: int, num_days: int | None = None,
                    time_limit_seconds: float = 30.0, now: datetime | None = None):
    """A continuous plan from `now` to the last deadline in play; num_days is only a minimum.
    Returns (anchor, fixed blocks, scheduled items, warnings)."""
    if not has_saved_items(conn, student_id):
        raise ValueError("no saved schedule items -- run import_schedule.py first")

    fit = build_fit_inputs(conn, student_id, now or datetime.now(), min_days=num_days or 1)
    items, unscheduled = fit.frame.solve(fit.tasks, time_limit_seconds)
    warnings = sleep_warnings(fit.sleep_rules, items) + task_warnings(unscheduled) + fit.warnings
    return fit.anchor, fit.fixed, items, warnings

def format_plan(anchor: PlanAnchor, fixed, items, warnings) -> list[str]:
    """Plain-text lines: fixed blocks and solved items merged by real date and time."""
    rows = [(b.day, b.start_slot, b.end_slot, "fixed", b.title) for b in fixed]
    rows += [(i.day, i.start_slot, i.end_slot, i.kind, i.title) for i in items if i.kind != "fixed"]
    rows.sort()
    lines, last_day = [], None
    for day, start, end, kind, title in rows:
        if day != last_day:
            lines.append(f"{anchor.date_of(day):%a %d %b %Y}")
            last_day = day
        lines.append(f"  {clock_range(start, end)}  [{kind}]  {title}")
    if warnings:
        lines.append("Warnings:")
        lines += [f"  [{w.severity}] {w.kind}: {w.message}" for w in warnings]
    return lines
