from datetime import date, timedelta

from scheduler.calendar_utils import build_plan_inputs, find_overlaps
from scheduler.db import (
    get_dated_blocks, get_extracted_tasks, get_weekly_patterns, load_settings,
)
from scheduler.models import FixedBlock, PlanAnchor, SLOTS_PER_DAY, slot_to_time
from scheduler.solver import build_schedule, sleep_warnings, task_warnings


def plan_from_saved(conn, student_id: int, num_days: int = 7, start_date: date | None = None,
                    time_limit_seconds: float = 30.0):  # NEW
    """Load the student's saved extracted items, anchor them to `start_date`, and solve.
    start_date defaults to TOMORROW: day 0 starts at 00:00, so planning from today could
    put tasks in hours that have already passed.
    Returns (anchor, fixed_blocks, scheduled_items, warnings)."""
    patterns = [p for _, p in get_weekly_patterns(conn, student_id)]
    dated = [b for _, b in get_dated_blocks(conn, student_id)]
    saved_tasks = [t for _, t in get_extracted_tasks(conn, student_id)]
    if not (patterns or dated or saved_tasks):
        raise ValueError("no saved schedule items -- run import_schedule.py first")

    anchor = PlanAnchor(start_date=start_date or date.today() + timedelta(days=1),
                        num_days=num_days)
    fixed, tasks = build_plan_inputs(patterns, dated, saved_tasks, anchor)
    overlaps = find_overlaps(fixed)  # NEW -- the solver only says "no valid schedule"
    if overlaps:
        lines = []
        for a, b in overlaps[:5]:
            d = anchor.start_date + timedelta(days=a.day)
            lines.append(f"  {d:%a %d %b}: '{a.title}' {slot_to_time(a.start_slot)}-"
                         f"{slot_to_time(a.end_slot % SLOTS_PER_DAY)} overlaps '{b.title}' "
                         f"{slot_to_time(b.start_slot)}-{slot_to_time(b.end_slot % SLOTS_PER_DAY)}")
        more = f"\n  ...and {len(overlaps) - 5} more" if len(overlaps) > 5 else ""
        raise ValueError("Some saved blocks overlap, so no schedule is possible:\n"
                         + "\n".join(lines) + more
                         + "\nRe-run import_schedule.py and delete or fix the duplicates.")
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