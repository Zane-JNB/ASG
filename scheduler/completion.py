from dataclasses import dataclass
from datetime import datetime, time, timedelta

from scheduler.db import (
    clear_plan_cut, clear_task_sessions, due_sessions, EXTRACTED_TASKS, load_settings,
    mark_sessions_asked, record_plan_sessions, reduce_plan_cut, transaction,
)
from scheduler.units import MINUTES_PER_SLOT, format_hours
from scheduler.models import PlanAnchor
from scheduler.prompts import confirm
from scheduler.restore import plan_restores
from scheduler.solver import FIT_CHECK_SECONDS
from scheduler.task_filter import wants_reminder


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


def finish_task(conn, student_id: int, task_id: int, now: datetime, ask=input, show=print,
                time_limit_seconds: float = FIT_CHECK_SECONDS, missed: bool = False) -> dict:
    """Close a task as done (or missed=True: closed without being done), kept as history either
    way; free its time, and offer to give cut tasks their hours back.
    Returns {"restored": {task id: slots}} -- empty if nothing was restored."""
    task = EXTRACTED_TASKS.find(conn, student_id, task_id)
    if task is None or task.completed_at:
        raise ValueError("that task is not an open task")
    with transaction(conn):  # closed together with its cut and sessions, or not at all
        EXTRACTED_TASKS.update(conn, student_id, task_id,
                               task.model_copy(update={"completed_at": _iso(now), "missed": missed}))
        clear_plan_cut(conn, student_id, task_id)
        clear_task_sessions(conn, student_id, task_id)
    show(f"Marked '{task.title}' as {'missed' if missed else 'done'}.")
    try:
        restores = plan_restores(conn, student_id, now, time_limit_seconds)
    except (ValueError, RuntimeError):
        return {"restored": {}}  # could not verify a restore, so offer none
    if not restores:
        return {"restored": {}}
    titles = {i: t.title for i, t in EXTRACTED_TASKS.get(conn, student_id)}
    show("That frees up time. These tasks can get hours back:")
    for tid, slots in restores.items():
        show(f"  - '{titles[tid]}' +{format_hours(slots)}")
    if not confirm(ask, "Restore them?", True):
        return {"restored": {}}
    with transaction(conn):
        for tid, slots in restores.items():
            reduce_plan_cut(conn, student_id, tid, slots)
    return {"restored": restores}


def record_plan(conn, student_id: int, anchor: PlanAnchor, items, now: datetime) -> int:
    """Remember when each task session of a plan is scheduled, so check-ins can tell which ones
    have passed. Call it after making a plan. Returns how many sessions were recorded."""
    origin = datetime.combine(anchor.start_date, time(0))
    rows = []
    for it in items:
        tid = it.saved_id if it.kind == "task" else None  # by id, so same-titled tasks stay apart
        if tid is None:
            continue
        start, end = (origin + timedelta(minutes=MINUTES_PER_SLOT * s) for s in it.span)
        rows.append((tid, _iso(start), _iso(end)))
    record_plan_sessions(conn, student_id, _iso(now), rows)
    return len(rows)


@dataclass
class Checkin:
    task_id: int
    title: str
    ended: datetime  # when the latest unanswered session ended


def _ended(conn, student_id: int, now: datetime) -> tuple[list[Checkin], set[int]]:
    """Tasks with ended, unasked sessions: (check-ins to ask, ids of tasks not to ask about --
    closed, deleted, or not wanted as a reminder)."""
    settings = load_settings(conn, student_id)
    tasks = dict(EXTRACTED_TASKS.get(conn, student_id))
    ask, quiet = {}, set()
    for task_id, _start, end in due_sessions(conn, student_id, _iso(now)):
        task = tasks.get(task_id)
        if task is None or task.completed_at or not wants_reminder(task, settings):
            quiet.add(task_id)
        else:
            ask[task_id] = Checkin(task_id, task.title, datetime.fromisoformat(end))  # the latest session wins
    return list(ask.values()), quiet


def due_checkins(conn, student_id: int, now: datetime) -> list[Checkin]:
    """One entry per task whose scheduled time has passed, if the student wants reminders for it."""
    return _ended(conn, student_id, now)[0]


def run_checkin(conn, student_id: int, now: datetime, ask=input, show=print) -> int:
    """Ask 'did you finish it?' for each task with passed time. Returns how many were asked.
    Ended sessions nobody will be asked about are let go, so they don't pile up for later."""
    checkins, quiet = _ended(conn, student_id, now)
    with transaction(conn):
        for task_id in quiet:
            mark_sessions_asked(conn, student_id, task_id, _iso(now))
    for c in checkins:
        show(f"Your time for '{c.title}' ended {c.ended:%a %d %b %H:%M}.")
        if confirm(ask, f"Did you finish '{c.title}'?", False):
            finish_task(conn, student_id, c.task_id, now, ask, show)
        else:
            mark_sessions_asked(conn, student_id, c.task_id, _iso(now))
    return len(checkins)
