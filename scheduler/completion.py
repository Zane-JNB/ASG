import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from scheduler.db import (
    clear_plan_cut, clear_task_sessions, due_sessions, get_extracted_tasks, load_settings,
    mark_sessions_asked, record_plan_sessions, reduce_plan_cut, update_extracted_task,
)
from scheduler.drop_review import _h
from scheduler.fit_check import planned_tasks
from scheduler.models import MINUTES_PER_SLOT, PlanAnchor, SLOTS_PER_DAY
from scheduler.restore import plan_restores
from scheduler.review import _confirm
from scheduler.task_filter import wants_reminder


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


def finish_task(conn, student_id: int, task_id: int, now: datetime, ask=input, show=print,
                time_limit_seconds: float = 5.0) -> dict:  # NEW
    """Mark a task done (kept as history), free its time, and offer to give cut tasks their hours
    back. Returns {"restored": {task id: slots}} -- empty if nothing was restored."""
    task = dict(get_extracted_tasks(conn, student_id)).get(task_id)
    if task is None or task.completed_at:
        raise ValueError("that task is not an open task")
    update_extracted_task(conn, student_id, task_id, task.model_copy(update={"completed_at": _iso(now)}))
    clear_plan_cut(conn, student_id, task_id)
    clear_task_sessions(conn, student_id, task_id)
    show(f"Marked '{task.title}' as done.")
    try:
        restores = plan_restores(conn, student_id, now, time_limit_seconds)
    except (ValueError, RuntimeError):
        return {"restored": {}}  # could not verify a restore, so offer none
    if not restores:
        return {"restored": {}}
    titles = {i: t.title for i, t in get_extracted_tasks(conn, student_id)}
    show("That frees up time. These tasks can get hours back:")
    for tid, slots in restores.items():
        show(f"  - '{titles[tid]}' +{_h(slots)}")
    if not _confirm(ask, "Restore them?", True):
        return {"restored": {}}
    for tid, slots in restores.items():
        reduce_plan_cut(conn, student_id, tid, slots)
    return {"restored": restores}


def record_plan(conn, student_id: int, anchor: PlanAnchor, items, now: datetime) -> int:  # NEW
    """Remember when each task session of a plan is scheduled, so check-ins can tell which ones
    have passed. Call it after making a plan. Returns how many sessions were recorded."""
    ids = {}
    for tid, task in planned_tasks(conn, student_id, PlanAnchor(start_date=anchor.start_date, num_days=1)):
        ids.setdefault(task.title, tid)
    origin = datetime.combine(anchor.start_date, time(0))
    rows = []
    for it in items:
        tid = ids.get(re.sub(r" \(\d+/\d+\)$", "", it.title)) if it.kind == "task" else None
        if tid is None:
            continue
        start = origin + timedelta(minutes=MINUTES_PER_SLOT * (it.day * SLOTS_PER_DAY + it.start_slot))
        end = origin + timedelta(minutes=MINUTES_PER_SLOT * (it.day * SLOTS_PER_DAY + it.end_slot))
        rows.append((tid, _iso(start), _iso(end)))
    record_plan_sessions(conn, student_id, _iso(now), rows)
    return len(rows)


@dataclass
class Checkin:  # NEW
    task_id: int
    title: str
    ended: str  # ISO time the latest unanswered session ended


def due_checkins(conn, student_id: int, now: datetime) -> list[Checkin]:  # NEW
    """One entry per task whose scheduled time has passed, if the student wants reminders for it."""
    settings = load_settings(conn, student_id)
    tasks = dict(get_extracted_tasks(conn, student_id))
    out = {}
    for task_id, _start, end in due_sessions(conn, student_id, _iso(now)):
        task = tasks.get(task_id)
        if task is None or task.completed_at or not wants_reminder(task, settings):
            continue
        out[task_id] = Checkin(task_id, task.title, end)
    return list(out.values())


def run_checkin(conn, student_id: int, now: datetime, ask=input, show=print) -> int:  # NEW
    """Ask 'did you finish it?' for each task with passed time. Returns how many were asked."""
    checkins = due_checkins(conn, student_id, now)
    for c in checkins:
        when = datetime.fromisoformat(c.ended)
        show(f"Your time for '{c.title}' ended {when:%a %d %b %H:%M}.")
        if _confirm(ask, f"Did you finish '{c.title}'?", False):
            finish_task(conn, student_id, c.task_id, now, ask, show)
        else:
            mark_sessions_asked(conn, student_id, c.task_id, _iso(now))
    return len(checkins)