"""A plan from the student's saved rows, and how it is printed."""
from datetime import datetime
from typing import NamedTuple

from scheduler.db import has_saved_items
from scheduler.fit_check import build_fit_inputs
from scheduler.models import FixedBlock, PlanAnchor, ScheduledItem, ScheduleWarning
from scheduler.solver import SOLVE_SECONDS, sleep_warnings, task_warnings
from scheduler.units import clock_range


class Plan(NamedTuple):
    anchor: PlanAnchor
    fixed: list[FixedBlock]  # what the solver planned around, incl. this morning's sleep
    items: list[ScheduledItem]  # everything placed, the fixed blocks included
    warnings: list[ScheduleWarning]


def plan_from_saved(conn, student_id: int, min_days: int = 1, time_limit_seconds: float = SOLVE_SECONDS,
                    *, now: datetime) -> Plan:
    """A continuous plan from `now` to the last deadline in play, at least min_days long."""
    if not has_saved_items(conn, student_id):
        raise ValueError("no saved schedule items -- run import_schedule.py first")

    fit = build_fit_inputs(conn, student_id, now, min_days=min_days)
    items, unscheduled = fit.frame.solve(fit.tasks, time_limit_seconds)
    warnings = sleep_warnings(fit.sleep_rules, items) + task_warnings(unscheduled) + fit.warnings
    return Plan(fit.anchor, fit.fixed, items, warnings)


def format_plan(plan: Plan) -> list[str]:
    """Plain-text lines: every item under its real date, by time, then the warnings."""
    lines, last_day = [], None
    for item in sorted(plan.items, key=lambda i: (i.day, i.start_slot, i.end_slot, i.kind, i.title)):
        if item.day != last_day:
            lines.append(f"{plan.anchor.date_of(item.day):%a %d %b %Y}")
            last_day = item.day
        lines.append(f"  {clock_range(item.start_slot, item.end_slot)}  [{item.kind}]  {item.title}")
    if plan.warnings:
        lines.append("Warnings:")
        lines += [f"  [{w.severity}] {w.kind}: {w.message}" for w in plan.warnings]
    return lines
