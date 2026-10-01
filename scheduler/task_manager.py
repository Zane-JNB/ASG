from datetime import date, datetime, time
from scheduler.add_with_fit import add_task_with_fit
from scheduler.db import (
    add_extracted_task, clear_extracted_tasks, delete_extracted_task, get_extracted_tasks,
    update_extracted_task, load_settings, save_settings, update_extracted_task
)
from scheduler.models import DynamicTask, ExtractedTask, MINUTES_PER_SLOT   
from scheduler.review import _confirm, _date, _describe, _hours
from tests.test_reflection_cycle import conn, student_id
from scheduler.completion import finish_task, run_checkin  # NEW
from scheduler.task_filter import describe_reminders  # NEW

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


def prompt_new_task(ask=input, show=print, today: date | None = None, session_cap: int | None = None) -> ExtractedTask:
    today = today or date.today()
    session_cap = session_cap or _SESSION_CAP

    def future_date(s: str) -> str:
        d = _date(s)
        if date.fromisoformat(d) < today:  # plans start tomorrow, so an earlier date is never planned
            raise ValueError("due date must be today     or later")
        return d

    title = _ask_field(ask, show, "title", str)
    due = _ask_field(ask, show, "due date YYYY-MM-DD", future_date)
    fields = {"title": title, "date": due}
    for label, key, parse, default in (
        ("hours", "duration_slots", _hours, f"{_DEFAULT.duration_slots * 15 / 60:g}"),
        ("priority 1-5", "priority", _rating, _DEFAULT.priority),
        ("difficulty 1-5", "difficulty", _rating, _DEFAULT.difficulty),
    ):
        value = _ask_field(ask, show, label, parse, default)
        if value is not None:
            fields[key] = value
        task = ExtractedTask(**fields)
    if task.duration_slots > session_cap:  # NEW -- splitting only matters for tasks longer than one session
        hours, cap = task.duration_slots * MINUTES_PER_SLOT / 60, session_cap * MINUTES_PER_SLOT / 60
        can = _confirm(ask, f"  {hours:g}h is longer than a {cap:g}h session. Can it be split across several sessions?", True)
        task = task.model_copy(update={"splittable": can})
    return task


def _sorted_tasks(conn, student_id):
    open_tasks = [(i, t) for i, t in get_extracted_tasks(conn, student_id) if not t.completed_at]  # NEW -- done = history
    return sorted(open_tasks, key=lambda x: (x[1].date, x[1].title.lower()))

def _ask_level(ask, show, prompt: str) -> int | None:  # NEW
    while True:
        raw = ask(prompt).strip()
        if not raw:
            return None
        if raw.isdigit() and 1 <= int(raw) <= 5:
            return int(raw)
        show("Enter a number from 1 to 5, or press Enter to ignore it.")


def _reminder_settings(conn, student_id, ask, show) -> None:  # NEW
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
    settings = settings.model_copy(update=update)
    save_settings(conn, student_id, settings)
    show(describe_reminders(settings))

def _show_tasks(tasks, show, session_cap: int | None = None):
    if not tasks:
        show("No tasks saved.")
    for n, (_, t) in enumerate(tasks, 1):
        show(f"{n}. {_describe('Task', t, session_cap)}")

def run_menu(conn, student_id, ask=input, show=print, today: date | None = None, 
             now: datetime | None = None) -> None:
    now = now or (datetime.combine(today, time(0, 0)) if today else datetime.now())
    today = today or now.date()  # NEW
    run_checkin(conn, student_id, now, ask, show)
    while True:
        choice = ask("Tasks: [a]dd  [l]ist  [d]elete one  [f]inished  [c]heck-in  [s]plit setting  session [t]ime  [r]eminders  [x] delete ALL  [q]uit: ").strip().lower()
        if choice == "q":
            return
        if choice == "a":
           cap = load_settings(conn, student_id).default_max_session_slots
           add_task_with_fit(conn, student_id, prompt_new_task(ask, show, today, cap), now, ask, show)
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
        elif choice == "f":  # NEW -- manual "mark task done"
            tasks = _sorted_tasks(conn, student_id)
            _show_tasks(tasks, show, load_settings(conn, student_id).default_max_session_slots)
            if not tasks:
                continue
            raw = ask("Number of the task you finished (Enter to cancel): ").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(tasks):
                finish_task(conn, student_id, tasks[int(raw) - 1][0], now, ask, show)
            elif raw:
                show(f"Enter a number between 1 and {len(tasks)}.")
        elif choice == "c":  # NEW
            if not run_checkin(conn, student_id, now, ask, show):
                show("Nothing to check in on right now.")
        elif choice == "r":  # NEW
            _reminder_settings(conn, student_id, ask, show)
        elif choice == "s":  # NEW
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
        elif choice == "t":  # NEW -- how long each session of a task lasts by default
            settings = load_settings(conn, student_id)
            now_h = settings.default_max_session_slots * MINUTES_PER_SLOT / 60
            raw = ask(f"Longest single session in hours [{now_h:g}] (Enter to keep): ").strip()
            if raw:
                try:
                    slots = _hours(raw)
                except ValueError as e:
                    show(f"Invalid: {e}")
                    continue
                save_settings(conn, student_id, settings.model_copy(update={"default_max_session_slots": slots}))
                show(f"Saved -- tasks are now planned in sessions of up to {slots * MINUTES_PER_SLOT / 60:g}h.")
        elif choice == "x":
            count = len(get_extracted_tasks(conn, student_id))
            if count == 0:
                show("No tasks saved.")
            elif ask(f"Delete ALL {count} task(s)? Type yes to confirm: ").strip().lower() == "yes":
                clear_extracted_tasks(conn, student_id)
                show(f"Deleted {count} task(s).")
            else:
                show("Cancelled -- nothing deleted.")
        else:
            show("Choose a, l, d, f, c, s, t, r, x or q.")