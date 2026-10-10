import math
from datetime import date, datetime 
from pydantic import ValidationError  
from scheduler.models import ExtractedTask, ExtractionResult, WeeklyPattern, DatedBlock
from scheduler.units import MINUTES_PER_SLOT, parse_due_time, parse_weekday, slots_to_hours, time_to_slot


def hours_to_slots(hours: float) -> int:   
    """1.5 -> 6. Rounds to the nearest 15 minutes (halves round up); minimum one slot."""
    slots = math.floor(hours * 60 / MINUTES_PER_SLOT + 0.5)
    if slots < 1:
        raise ValueError("duration must be at least 15 minutes")
    return slots


def edit_extracted_task(task: ExtractedTask, hours: float | None = None,   
                        priority: int | None = None, difficulty: int | None = None) -> ExtractedTask:
    """Return a copy of task with the given placeholders replaced (None = keep as is)."""
    changes = {}
    if hours is not None:
        changes["duration_slots"] = hours_to_slots(hours)
    if priority is not None:
        changes["priority"] = priority
    if difficulty is not None:
        changes["difficulty"] = difficulty
    return ExtractedTask.model_validate({**task.model_dump(), **changes})

def _confirm(ask, prompt: str, default: bool) -> bool:  
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

def _missed(ask, show, prompt: str) -> bool:
    """'Was it [d]one or [m]issed?' -- True for missed. Enter = done; anything else re-asks."""
    while True:
        raw = ask(f"{prompt} Was it [d]one or [m]issed? (Enter = done): ").strip().lower()
        if raw in ("", "d", "m"):
            return raw == "m"
        show("Type d or m.")

def _time(s: str) -> str:  #   -- "9:00" -> "09:00"; ValueError if not a real time
    return datetime.strptime(s, "%H:%M").strftime("%H:%M")


def _due(s: str) -> tuple[str, str | None]:  #   -- "2026-10-02" or "2026-10-02 10:00"
    """(date, due time or None). A date alone means due at the end of that day."""
    day, _, clock = s.strip().partition(" ")
    clock = clock.strip()
    if not clock:
        return _date(day), None
    return _date(day), parse_due_time(clock)


def _date(s: str) -> str:  #   -- the models store dates as plain strings, so check them here
    return date.fromisoformat(s).isoformat()


def _hours(s: str) -> int:  #   -- typed as hours, stored as slots
    return hours_to_slots(float(s))


#   -- what can be edited per item type: (prompt label, model field, parser)
_EDIT_FIELDS = {
    WeeklyPattern: [("title", "title", str), ("day", "day", parse_weekday),
                    ("start HH:MM", "start_time", _time), ("end HH:MM", "end_time", _time)],
    DatedBlock: [("title", "title", str), ("date YYYY-MM-DD", "date", _date),
                 ("start HH:MM", "start_time", _time), ("end HH:MM", "end_time", _time)],
    ExtractedTask: [("title", "title", str), ("due YYYY-MM-DD [HH:MM]", "date", _due),
                    ("hours", "duration_slots", _hours), ("priority 1-5", "priority", int),
                    ("difficulty 1-5", "difficulty", int)],
}


def _current(item, key: str):   
    value = getattr(item, key)
    if key == "date" and isinstance(item, ExtractedTask):
        return item.due_label()
    return f"{slots_to_hours(value):g}" if key == "duration_slots" else value


def _describe(kind: str, item, session_cap: int | None = None) -> str:   
    if kind == "Task":
        hours = slots_to_hours(item.duration_slots)
        if not item.splittable:   
            note = ", one block"
        elif session_cap and item.duration_slots > session_cap:
            note = ", can be split"
        else:
            note = ""
        return f"{item.title} due {item.due_label()} ({hours:g}h, priority {item.priority}, difficulty {item.difficulty}{note})"
    when = f"{item.day}" if kind == "Weekly" else f"{item.date}"
    return f"{item.title} -- {when} {item.start_time}-{item.end_time}"


def _edit_item(item, ask, show, session_cap: int | None):   
    """Ask for each field (Enter keeps the current value). Bad input shows why and re-asks."""
    while True:
        try:
            changes = {}
            for label, key, parse in _EDIT_FIELDS[type(item)]:
                raw = ask(f"  {label} [{_current(item, key)}]: ").strip()
                if raw and parse is _due:  #   -- one answer sets the date and the (optional) time
                    changes["date"], changes["due_time"] = parse(raw)
                    if item.due_time and changes["due_time"] is None:
                        show(f"  Due time {item.due_time} removed: now due at the end of that day.")
                elif raw:
                    changes[key] = parse(raw)
            if isinstance(item, ExtractedTask) and session_cap:  #   -- only long tasks can be split
                if changes.get("duration_slots", item.duration_slots) > session_cap:
                    cap_h = slots_to_hours(session_cap)
                    changes["splittable"] = _confirm(
                        ask, f"  Can it be split into sessions of up to {cap_h:g}h? (n = one block)", item.splittable)
            return type(item).model_validate({**item.model_dump(), **changes})
        except ValueError as e:  # parser errors AND pydantic's ValidationError
            msg = e.errors()[0]["msg"] if isinstance(e, ValidationError) else str(e)
            show(f"  Invalid: {msg} -- try again.")


def _parse_picks(text: str, count: int) -> list[int]:   
    picks = sorted({int(x) for x in text.replace(",", " ").split()})
    if any(n < 1 or n > count for n in picks):
        raise ValueError
    return picks

def _overlap_notes(items) -> list[str]:   
    """Numbered items that clash: weekly ones on the same weekday, sessions on the same date."""
    notes = []
    for i, (kind_a, a) in enumerate(items):
        for j in range(i + 1, len(items)):
            kind_b, b = items[j]
            if kind_a != kind_b or kind_a == "Task":
                continue
            same_day = a.day == b.day if kind_a == "Weekly" else a.date == b.date
            if same_day and (time_to_slot(a.start_time) < time_to_slot(b.end_time)
                             and time_to_slot(b.start_time) < time_to_slot(a.end_time)):
                notes.append(f"{i + 1} and {j + 1}")
    return notes
       
    
        
def review_extraction(result: ExtractionResult, ask=input, show=print, session_cap: int | None = None) -> ExtractionResult:   
    """Show everything found, then ONE prompt: Enter accepts all, or pick numbers to edit/delete."""
    items = ([("Weekly", p) for p in result.weekly_patterns]
             + [("Session", b) for b in result.dated_blocks]
             + [("Task", t) for t in result.tasks])
    if not items:
        return ExtractionResult()

    show("Found:")
    for n, (kind, item) in enumerate(items, 1):
        show(f"{n}. [{kind}] {_describe(kind, item)}")
    clashes = _overlap_notes(items)
    if clashes:
        show("WARNING -- these overlap in time (often the same class listed twice): "
             + "; ".join(clashes))
    if result.tasks:
        show("Task hours/priority/difficulty are placeholder guesses -- fix any that are off.")
        if session_cap and any(t.duration_slots > session_cap for t in result.tasks):   
            cap_h = slots_to_hours(session_cap)
            show(f"Tasks longer than {cap_h:g}h are marked 'can be split' into sessions -- "
                 "pick one and edit it to make it a single block instead.")

    while True:
        answer = ask("Numbers to fix or delete (e.g. 2 5), or Enter to accept all: ")
        try:
            picks = _parse_picks(answer, len(items))
            break
        except ValueError:
            show(f"Enter numbers between 1 and {len(items)}, like: 2 5")

    for n in picks:
        kind, item = items[n - 1]
        show(f"{n}. [{kind}] {_describe(kind, item)}")
        while True:
            action = ask("  [e]dit, [d]elete, or Enter to leave as is: ").strip().lower()
            if action in ("", "e", "d"):
                break
        if action == "d":
            items[n - 1] = None
        elif action == "e":
            items[n - 1] = (kind, _edit_item(item, ask, show, session_cap))    
    kept = [x for x in items if x is not None]
    return ExtractionResult(
        weekly_patterns=[i for k, i in kept if k == "Weekly"],
        dated_blocks=[i for k, i in kept if k == "Session"],
        tasks=[i for k, i in kept if k == "Task"],
    )