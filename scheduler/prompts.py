"""The CLI menus' shared prompts, answer parsers and task description. Every menu asks, parses
and describes through these, so a rule (what Enter does, how hours are typed) lives in one place.
Parsers take the typed text and raise ValueError with a message the student can act on."""
from scheduler.models import ExtractedTask
from scheduler.units import hours_to_slots, parse_date, parse_due_time, slots_to_hours


def ask_until(ask, show, label: str, parse, default=None, required: bool = False):
    """Ask until parse() accepts. Enter returns `default` (None = cancel when there is none);
    with required=True, Enter says so and asks again."""
    while True:
        raw = ask(f"  {label}: ").strip()
        if not raw and required:
            show(f"  {label} is required.")
            continue
        if not raw:
            return default
        try:
            return parse(raw)
        except ValueError as e:
            show(f"  Invalid: {e}")


def pick(ask, show, items, prompt: str):
    """Let the student choose one of items by its 1-based number. Enter (or a bad number) returns None."""
    raw = ask(prompt).strip()
    if raw.isdigit() and 1 <= int(raw) <= len(items):
        return items[int(raw) - 1]
    if raw:
        show(f"Enter a number between 1 and {len(items)}.")
    return None


def confirm(ask, prompt: str, default: bool) -> bool:
    """Y/n question. Enter = default; anything unrecognised re-asks."""
    hint = " [Y/n] " if default else " [y/N] "
    while True:
        answer = ask(prompt + hint).strip().lower()
        if answer == "":
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def ask_missed(ask, show, prompt: str) -> bool:
    """'Was it [d]one or [m]issed?' -- True for missed. Enter = done; anything else re-asks."""
    while True:
        raw = ask(f"{prompt} Was it [d]one or [m]issed? (Enter = done): ").strip().lower()
        if raw in ("", "d", "m"):
            return raw == "m"
        show("Type d or m.")


def parse_whole(text: str, lo: int, hi: int) -> int:
    """A whole number from lo to hi."""
    try:
        n = int(text)
    except ValueError:
        n = None
    if n is None or not lo <= n <= hi:
        raise ValueError(f"enter a whole number from {lo} to {hi}")
    return n


def parse_rating(text: str) -> int:
    """A priority or difficulty, 1-5."""
    return parse_whole(text, 1, 5)


def parse_hours(text: str) -> int:
    """Hours as typed ('1.5') -> slots, rounded to the nearest 15 minutes, at least one."""
    try:
        hours = float(text)
    except ValueError:
        raise ValueError("enter hours as a number, like 1.5") from None
    return hours_to_slots(hours)


def parse_due(text: str) -> tuple[str, str | None]:
    """'2026-10-02' or '2026-10-02 10:00' -> (date, due time or None). A date alone means due at
    the end of that day; a time is rounded down to its 15-minute slot."""
    day, _, clock = text.strip().partition(" ")
    clock = clock.strip()
    return parse_date(day), (parse_due_time(clock) if clock else None)


def describe_task(task: ExtractedTask, session_cap: int | None = None) -> str:
    """One line per task, as every task list shows it. With session_cap, a task longer than one
    session says it can be split."""
    if not task.splittable:
        note = ", one block"
    elif session_cap and task.duration_slots > session_cap:
        note = ", can be split"
    else:
        note = ""
    if task.may_cut_sleep:
        note += ", may use sleep below target"
    return (f"{task.title} due {task.due_label()} ({slots_to_hours(task.duration_slots):g}h, "
            f"priority {task.priority}, difficulty {task.difficulty}{note})")
