from datetime import date

from scheduler.db import (
    add_extracted_task, clear_extracted_tasks, delete_extracted_task, get_extracted_tasks,
)
from scheduler.models import ExtractedTask
from scheduler.review import _confirm, _date, _describe, _hours

_DEFAULT = ExtractedTask(title="x", date="2000-01-01")  # only used to read the placeholder defaults


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


def prompt_new_task(ask=input, show=print, today: date | None = None) -> ExtractedTask:
    today = today or date.today()

    def future_date(s: str) -> str:
        d = _date(s)
        if date.fromisoformat(d) <= today:  # plans start tomorrow, so an earlier date is never planned
            raise ValueError("due date must be tomorrow or later")
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
    return ExtractedTask(**fields)


def _sorted_tasks(conn, student_id):
    return sorted(get_extracted_tasks(conn, student_id), key=lambda x: (x[1].date, x[1].title.lower()))


def _show_tasks(tasks, show):
    if not tasks:
        show("No tasks saved.")
    for n, (_, t) in enumerate(tasks, 1):
        show(f"{n}. {_describe('Task', t)}")


def run_menu(conn, student_id, ask=input, show=print, today: date | None = None) -> None:
    while True:
        choice = ask("Tasks: [a]dd  [l]ist  [d]elete one  [x] delete ALL  [q]uit: ").strip().lower()
        if choice == "q":
            return
        if choice == "a":
            add_extracted_task(conn, student_id, prompt_new_task(ask, show, today))
            show("Added.")
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
            show("Choose a, l, d, x or q.")