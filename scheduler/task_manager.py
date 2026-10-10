from datetime import date, datetime, time
from scheduler.add_with_fit import add_task_with_fit
from scheduler.db import (
    delete_extracted_task, delete_unreadable_item, get_extracted_tasks, get_unreadable_items,
    update_extracted_task, load_settings
)
from scheduler.models import DynamicTask, ExtractedTask
from scheduler.units import parse_time, slots_to_hours
from scheduler.review import _confirm, _describe, _due, _hours, _missed
from scheduler.completion import finish_task, run_checkin   
from scheduler.task_filter import describe_reminders   
from scheduler.commute_menu import run_commute_menu
from scheduler.settings_menu import run_settings_menu
from scheduler.preferences import Actor, PreferenceError, set_values

_DEFAULT = ExtractedTask(title="x", date="2000-01-01")  # only used to read the placeholder defaults
_SESSION_CAP = DynamicTask.model_fields["max_session_slots"].default

def _rating(s: str) -> int:
    n = int(s)
    if not 1 <= n <= 5:
        raise ValueError("enter a number from 1 to 5")
    return n


def _ask_field(ask, show, label: str, parse, default=None):
    """Ask until parse() accepts. Enter returns None (= keep the default) when a default exists."""
    hint = f" [{default}]" if default is not None else ""
    while True:
        raw = ask(f"  {label}{hint}: ").strip()
        if not raw:
            if default is not None:
                return None
            show(f"  {label} is required.")
            continue
        try:
            return parse(raw)
        except ValueError as e:
            show(f"  Invalid: {e}")


def prompt_new_task(ask=input, show=print, today: date | None = None, session_cap: int | None = None,
                    now: datetime | None = None) -> ExtractedTask:
    today = today or (now.date() if now else date.today())
    session_cap = session_cap or _SESSION_CAP

    def future_due(s: str) -> tuple[str, str | None]:
        d, t = _due(s)
        typed = ExtractedTask(title="x", date=d, due_time=t)  # midnight / rounding applied
        if now is not None and typed.due_at() <= now:
            raise ValueError(f"due {typed.due_label()} has already passed (plans run in 15-minute steps)")
        if date.fromisoformat(typed.date) < today:  # plans start from today, so an earlier date is never planned
            raise ValueError("due date must be today or later")
        clock = s.strip().partition(" ")[2].strip()
        if clock and parse_time(clock, end=True) != typed.due_time or typed.date != d:
            show(f"  Saved as due {typed.due_label()} (plans run in 15-minute steps).")
        return typed.date, typed.due_time

    title = _ask_field(ask, show, "title", str)
    due, due_time = _ask_field(ask, show, "due date YYYY-MM-DD (add HH:MM if it has a time)", future_due)
    fields = {"title": title, "date": due, "due_time": due_time}
    for label, key, parse, default in (
        ("hours", "duration_slots", _hours, f"{slots_to_hours(_DEFAULT.duration_slots):g}"),
        ("priority 1-5", "priority", _rating, _DEFAULT.priority),
        ("difficulty 1-5", "difficulty", _rating, _DEFAULT.difficulty),
    ):
        value = _ask_field(ask, show, label, parse, default)
        if value is not None:
            fields[key] = value
    task = ExtractedTask(**fields)
    if task.duration_slots > session_cap:  #   -- splitting only matters for tasks longer than one session
        hours, cap = slots_to_hours(task.duration_slots), slots_to_hours(session_cap)
        can = _confirm(ask, f"  {hours:g}h is longer than a {cap:g}h session. Can it be split across several sessions?", True)
        task = task.model_copy(update={"splittable": can})
    return task


def _sorted_tasks(conn, student_id):
    open_tasks = [(i, t) for i, t in get_extracted_tasks(conn, student_id) if not t.completed_at]  #   -- done = history
    return sorted(open_tasks, key=lambda x: (x[1].date, x[1].due_slot(), x[1].title.lower()))

def _ask_level(ask, show, prompt: str) -> int | None:   
    while True:
        raw = ask(prompt).strip()
        if not raw:
            return None
        if raw.isdigit() and 1 <= int(raw) <= 5:
            return int(raw)
        show("Enter a number from 1 to 5, or press Enter to ignore it.")


def _reminder_settings(conn, student_id, ask, show) -> None:   
    settings = load_settings(conn, student_id)
    show(describe_reminders(settings))
    choice = ask("Change: [a]ll tasks  [c]ustom  [o]ff  (Enter to keep): ").strip().lower()
    if choice == "a":
        update = {"reminders_enabled": True, "reminder_min_difficulty": None, "reminder_min_priority": None}
    elif choice == "o":
        update = {"reminders_enabled": False}
    elif choice == "c":
        d = _ask_level(ask, show, "Remind me about difficulty at or above (1-5, Enter to ignore difficulty): ")
        p = _ask_level(ask, show, "Remind me about priority at or above (1-5, Enter to ignore priority): ")
        update = {"reminders_enabled": True, "reminder_min_difficulty": d, "reminder_min_priority": p}
    else:
        if choice:
            show("Choose a, c or o.")
        return
    try:
        settings = set_values(conn, student_id, update, Actor.USER)
    except PreferenceError as e:
        show(str(e))
        return
    show(describe_reminders(settings))

def _show_tasks(tasks, show, session_cap: int | None = None):
    if not tasks:
        show("No tasks saved.")
    for n, (_, t) in enumerate(tasks, 1):
        show(f"{n}. {_describe('Task', t, session_cap)}")

def _unreadable_menu(conn, student_id, ask, show) -> None:
    """Saved rows that no longer pass their checks are skipped by the planner; list them and
    let the student delete one (nothing is deleted without asking)."""
    bad = get_unreadable_items(conn, student_id)
    if not bad:
        show("Every saved item can be read.")
        return
    for n, (label, row_id, reason) in enumerate(bad, 1):
        show(f"{n}. Saved {label} (id {row_id}): {reason}")
    raw = ask("Number to delete (Enter to keep them all): ").strip()
    if raw.isdigit() and 1 <= int(raw) <= len(bad):
        label, row_id, _ = bad[int(raw) - 1]
        delete_unreadable_item(conn, student_id, label, row_id)
        show("Deleted.")
    elif raw:
        show(f"Enter a number between 1 and {len(bad)}.")

def run_menu(conn, student_id, ask=input, show=print, today: date | None = None, 
             now: datetime | None = None, clock=None) -> None:
    """A fixed today/now (tests) is used throughout; otherwise the time is read again for every
    action, so a menu left open for hours doesn't plan from when it was opened."""
    fixed_now = now or (datetime.combine(today, time(0, 0)) if today else None)
    clock = (lambda: fixed_now) if fixed_now else (clock or datetime.now)
    fixed_today = today
    now = clock()
    today = fixed_today or now.date()
    run_checkin(conn, student_id, now, ask, show)
    while True:
        choice = ask("Tasks: [a]dd  [l]ist  [d]elete one  [f]inished/missed  [c]heck-in  [s]plit setting  session [t]ime  [r]eminders  [m] commutes  [p] settings  [u]nreadable  [x] delete ALL open  [q]uit: ").strip().lower()
        now = clock()
        today = fixed_today or now.date()
        if choice == "q":
            return
        if choice == "a":
            cap = load_settings(conn, student_id).default_max_session_slots
            add_task_with_fit(conn, student_id, prompt_new_task(ask, show, today, cap, now), now, ask, show)
        elif choice == "l":
            _show_tasks(_sorted_tasks(conn, student_id), show)
        elif choice == "d":
            tasks = _sorted_tasks(conn, student_id)
            _show_tasks(tasks, show)
            if not tasks:
                continue    
            raw = ask("Number to delete (Enter to cancel): ").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(tasks):
                delete_extracted_task(conn, student_id, tasks[int(raw) - 1][0])
                show("Deleted.")
            elif raw:
                show(f"Enter a number between 1 and {len(tasks)}.")
        elif choice == "f":  #   -- close a task: done, or missed (both kept as history)
            tasks = _sorted_tasks(conn, student_id)
            _show_tasks(tasks, show, load_settings(conn, student_id).default_max_session_slots)
            if not tasks:
                continue
            raw = ask("Number of the task to close (Enter to cancel): ").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(tasks):
                task_id, task = tasks[int(raw) - 1]
                missed = _missed(ask, show, f"'{task.title}':")
                finish_task(conn, student_id, task_id, now, ask, show, missed=missed)
            elif raw:
                show(f"Enter a number between 1 and {len(tasks)}.")
        elif choice == "c":   
            if not run_checkin(conn, student_id, now, ask, show):
                show("Nothing to check in on right now.")
        elif choice == "r":   
            _reminder_settings(conn, student_id, ask, show)
        elif choice == "s":   
            tasks = _sorted_tasks(conn, student_id)
            _show_tasks(tasks, show)
            if not tasks:
                continue
            raw = ask("Number to change (Enter to cancel): ").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(tasks):
                task_id, task = tasks[int(raw) - 1]
                can = _confirm(ask, f"  Can '{task.title}' be split across several sessions?", task.splittable)
                update_extracted_task(conn, student_id, task_id, task.model_copy(update={"splittable": can}))
                show("Saved." if can else "Saved -- it will be planned as one block.")
            elif raw:
                show(f"Enter a number between 1 and {len(tasks)}.")
        elif choice == "t":  #   -- how long each session of a task lasts by default
            settings = load_settings(conn, student_id)
            now_h = slots_to_hours(settings.default_max_session_slots)
            raw = ask(f"Longest single session in hours [{now_h:g}] (Enter to keep): ").strip()
            if raw:
                try:
                    slots = _hours(raw)
                except ValueError as e:
                    show(f"Invalid: {e}")
                    continue
                try:
                    set_values(conn, student_id, {"default_max_session_slots": slots}, Actor.USER)
                except PreferenceError as e:
                    show(str(e))
                    continue
                show(f"Saved -- tasks are now planned in sessions of up to {slots_to_hours(slots):g}h.")
        elif choice == "m":   
            run_commute_menu(conn, student_id, ask, show, today)
        elif choice == "p":                                        
            run_settings_menu(conn, student_id, ask, show)
        elif choice == "u":
            _unreadable_menu(conn, student_id, ask, show)
        elif choice == "x":
            open_tasks = _sorted_tasks(conn, student_id)  #   -- completed tasks stay as history
            count = len(open_tasks)
            if count == 0:
                show("No open tasks saved.")
            elif ask(f"Delete ALL {count} open task(s)? Completed ones are kept. Type yes to confirm: ").strip().lower() == "yes":
                for task_id, _ in open_tasks:
                    delete_extracted_task(conn, student_id, task_id)
                show(f"Deleted {count} open task(s).")
            else:
                show("Cancelled -- nothing deleted.")
        else:
            show("Choose a, l, d, f, c, s, t, r, m, p, u, x or q.")