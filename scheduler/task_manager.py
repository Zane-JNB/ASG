import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time

from scheduler.add_with_fit import add_task_with_fit
from scheduler.commute_menu import run_commute_menu
from scheduler.completion import finish_task, run_checkin
from scheduler.db import EXTRACTED_TASKS, get_unreadable_items, load_settings, transaction
from scheduler.models import ExtractedTask, ProfileSettings
from scheduler.preferences import Actor, PreferenceError, set_values
from scheduler.prompts import (ask_missed, ask_until, confirm, describe_task, parse_due, parse_hours,
                               parse_rating, pick)
from scheduler.settings_menu import run_settings_menu
from scheduler.task_filter import describe_reminders
from scheduler.units import parse_time, plan_start, slots_to_hours

_DEFAULT = {name: ExtractedTask.model_fields[name].default for name in ("duration_slots", "priority", "difficulty")}


def prompt_new_task(ask=input, show=print, *, now: datetime, session_cap: int) -> ExtractedTask:
    """Ask for a new task. Its due time must leave at least one slot to plan it in."""

    def future_due(s: str) -> tuple[str, str | None]:
        d, t = parse_due(s)
        typed = ExtractedTask(title="x", date=d, due_time=t)  # midnight / rounding applied
        if typed.due_at() <= plan_start(now):  # no slot left to plan it in (an earlier date included)
            raise ValueError(f"due {typed.due_label()} has already passed (plans run in 15-minute steps)")
        clock = s.strip().partition(" ")[2].strip()
        if clock and parse_time(clock, end=True) != typed.due_time or typed.date != d:
            show(f"  Saved as due {typed.due_label()} (plans run in 15-minute steps).")
        return typed.date, typed.due_time

    title = ask_until(ask, show, "title", str, required=True)
    due, due_time = ask_until(ask, show, "due date YYYY-MM-DD (add HH:MM if it has a time)", future_due,
                              required=True)
    task = ExtractedTask(
        title=title, date=due, due_time=due_time,
        duration_slots=ask_until(ask, show, f"hours [{slots_to_hours(_DEFAULT['duration_slots']):g}]", parse_hours,
                                 _DEFAULT["duration_slots"]),
        priority=ask_until(ask, show, f"priority 1-5 [{_DEFAULT['priority']}]", parse_rating, _DEFAULT["priority"]),
        difficulty=ask_until(ask, show, f"difficulty 1-5 [{_DEFAULT['difficulty']}]", parse_rating,
                             _DEFAULT["difficulty"]),
    )
    if task.duration_slots > session_cap:  # splitting only matters for tasks longer than one session
        hours, cap = slots_to_hours(task.duration_slots), slots_to_hours(session_cap)
        can = confirm(ask, f"  {hours:g}h is longer than a {cap:g}h session. Can it be split across several sessions?", True)
        task = task.model_copy(update={"splittable": can})
    return task


def _sorted_tasks(conn, student_id: int) -> list[tuple[int, ExtractedTask]]:
    open_tasks = [(i, t) for i, t in EXTRACTED_TASKS.get(conn, student_id) if not t.completed_at]  # done = history
    return sorted(open_tasks, key=lambda x: (x[1].date, x[1].due_slot(), x[1].title.lower()))


@dataclass(frozen=True)
class _Menu:
    """What every menu action needs. now is read again before each action."""
    conn: sqlite3.Connection
    student_id: int
    ask: Callable[[str], str]
    show: Callable[[str], object]
    now: datetime

    @property
    def today(self) -> date:
        return self.now.date()

    def session_cap(self) -> int:
        return load_settings(self.conn, self.student_id).default_max_session_slots


def _show_tasks(tasks: list[tuple[int, ExtractedTask]], show, session_cap: int | None = None) -> None:
    if not tasks:
        show("No tasks saved.")
    for n, (_, t) in enumerate(tasks, 1):
        show(f"{n}. {describe_task(t, session_cap)}")


def _pick_task(m: _Menu, prompt: str, session_cap: int | None = None) -> tuple[int, ExtractedTask] | None:
    """List the open tasks and let the student pick one: (task id, task), or None."""
    tasks = _sorted_tasks(m.conn, m.student_id)
    _show_tasks(tasks, m.show, session_cap)
    return pick(m.ask, m.show, tasks, prompt) if tasks else None


def _add(m: _Menu) -> None:
    task = prompt_new_task(m.ask, m.show, now=m.now, session_cap=m.session_cap())
    add_task_with_fit(m.conn, m.student_id, task, m.now, m.ask, m.show)


def _list(m: _Menu) -> None:
    _show_tasks(_sorted_tasks(m.conn, m.student_id), m.show)


def _delete(m: _Menu) -> None:
    picked = _pick_task(m, "Number to delete (Enter to cancel): ")
    if picked:
        EXTRACTED_TASKS.delete(m.conn, m.student_id, picked[0])
        m.show("Deleted.")


def _close(m: _Menu) -> None:
    """Close a task: done, or missed (both kept as history)."""
    picked = _pick_task(m, "Number of the task to close (Enter to cancel): ", m.session_cap())
    if picked:
        task_id, task = picked
        missed = ask_missed(m.ask, m.show, f"'{task.title}':")
        finish_task(m.conn, m.student_id, task_id, m.now, m.ask, m.show, missed=missed)


def _checkin(m: _Menu) -> None:
    if not run_checkin(m.conn, m.student_id, m.now, m.ask, m.show):
        m.show("Nothing to check in on right now.")


def _task_settings(m: _Menu) -> None:
    """The student's per-task choices: split into sessions, and leave to use sleep below target."""
    picked = _pick_task(m, "Number to change (Enter to cancel): ")
    if not picked:
        return
    task_id, task = picked
    can = confirm(m.ask, f"  Can '{task.title}' be split across several sessions?", task.splittable)
    sleep = confirm(m.ask, f"  May '{task.title}' use sleep below your target (never below your minimum)?",
                    task.may_cut_sleep)
    EXTRACTED_TASKS.update(m.conn, m.student_id, task_id,
                           task.model_copy(update={"splittable": can, "may_cut_sleep": sleep}))
    m.show("Saved." if can else "Saved -- it will be planned as one block.")


def _save_settings(m: _Menu, update: dict) -> ProfileSettings | None:
    """Save settings as the student; a refusal is shown and gives None."""
    try:
        return set_values(m.conn, m.student_id, update, Actor.USER)
    except PreferenceError as e:
        m.show(str(e))
        return None


def _session_time(m: _Menu) -> None:
    """How long each session of a task lasts by default. One answer; junk goes back to the menu."""
    raw = m.ask(f"Longest single session in hours [{slots_to_hours(m.session_cap()):g}] (Enter to keep): ").strip()
    if not raw:
        return
    try:
        slots = parse_hours(raw)
    except ValueError as e:
        m.show(f"Invalid: {e}")
        return
    if _save_settings(m, {"default_max_session_slots": slots}):
        m.show(f"Saved -- tasks are now planned in sessions of up to {slots_to_hours(slots):g}h.")


def _ask_level(m: _Menu, what: str) -> int | None:
    return ask_until(m.ask, m.show, f"Remind me about {what} at or above (1-5, Enter to ignore {what})",
                     parse_rating)


def _reminders(m: _Menu) -> None:
    m.show(describe_reminders(load_settings(m.conn, m.student_id)))
    choice = m.ask("Change: [a]ll tasks  [c]ustom  [o]ff  (Enter to keep): ").strip().lower()
    if choice == "a":
        update = {"reminders_enabled": True, "reminder_min_difficulty": None, "reminder_min_priority": None}
    elif choice == "o":
        update = {"reminders_enabled": False}
    elif choice == "c":
        update = {"reminders_enabled": True, "reminder_min_difficulty": _ask_level(m, "difficulty"),
                  "reminder_min_priority": _ask_level(m, "priority")}
    else:
        if choice:
            m.show("Choose a, c or o.")
        return
    settings = _save_settings(m, update)
    if settings:
        m.show(describe_reminders(settings))


def _commutes(m: _Menu) -> None:
    run_commute_menu(m.conn, m.student_id, m.ask, m.show, today=m.today)


def _settings(m: _Menu) -> None:
    run_settings_menu(m.conn, m.student_id, m.ask, m.show)


def _unreadable(m: _Menu) -> None:
    """Saved rows that no longer pass their checks are skipped by the planner; list them and
    let the student delete one (nothing is deleted without asking)."""
    bad = get_unreadable_items(m.conn, m.student_id)
    if not bad:
        m.show("Every saved item can be read.")
        return
    for n, u in enumerate(bad, 1):
        m.show(f"{n}. Saved {u.table.label} (id {u.row_id}): {u.reason}")
    u = pick(m.ask, m.show, bad, "Number to delete (Enter to keep them all): ")
    if u:
        u.table.delete(m.conn, m.student_id, u.row_id)
        m.show("Deleted.")


def _delete_all(m: _Menu) -> None:
    open_tasks = _sorted_tasks(m.conn, m.student_id)  # completed tasks stay as history
    if not open_tasks:
        m.show("No open tasks saved.")
        return
    count = len(open_tasks)
    answer = m.ask(f"Delete ALL {count} open task(s)? Completed ones are kept. Type yes to confirm: ")
    if answer.strip().lower() != "yes":
        m.show("Cancelled -- nothing deleted.")
        return
    with transaction(m.conn):  # all of them, or none
        for task_id, _ in open_tasks:
            EXTRACTED_TASKS.delete(m.conn, m.student_id, task_id)
    m.show(f"Deleted {count} open task(s).")


_ACTIONS = {  # key -> (menu label, handler), in menu order
    "a": ("[a]dd", _add),
    "l": ("[l]ist", _list),
    "d": ("[d]elete one", _delete),
    "f": ("[f]inished/missed", _close),
    "c": ("[c]heck-in", _checkin),
    "s": ("[s] task split/sleep", _task_settings),
    "t": ("session [t]ime", _session_time),
    "r": ("[r]eminders", _reminders),
    "m": ("[m] commutes", _commutes),
    "p": ("[p] settings", _settings),
    "u": ("[u]nreadable", _unreadable),
    "x": ("[x] delete ALL open", _delete_all),
}
_PROMPT = "Tasks: " + "  ".join(label for label, _ in _ACTIONS.values()) + "  [q]uit: "
_CHOOSE = f"Choose {', '.join(_ACTIONS)} or q."


def run_menu(conn, student_id: int, ask=input, show=print, today: date | None = None,
             now: datetime | None = None, clock: Callable[[], datetime] = datetime.now) -> None:
    """clock is read again before every action, so a menu left open for hours doesn't plan from
    when it was opened. A fixed now, or today at midnight (tests), replaces it."""
    fixed_now = now or (datetime.combine(today, time(0, 0)) if today else None)
    if fixed_now:
        clock = lambda: fixed_now  # noqa: E731
    run_checkin(conn, student_id, clock(), ask, show)
    while True:
        choice = ask(_PROMPT).strip().lower()
        if choice == "q":
            return
        if choice not in _ACTIONS:
            show(_CHOOSE)
            continue
        _ACTIONS[choice][1](_Menu(conn, student_id, ask, show, clock()))
