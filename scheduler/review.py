"""Review of an extraction before it is saved: list everything found, then edit or delete by number."""
from pydantic import ValidationError

from scheduler.models import ExtractedTask, ExtractionResult, WeeklyPattern, DatedBlock
from scheduler.prompts import confirm, describe_task, parse_due, parse_hours, parse_rating
from scheduler.units import parse_date, parse_time, parse_weekday, slots_to_hours, time_to_slot


#   -- what can be edited per item type: (prompt label, model field, parser)
_EDIT_FIELDS = {
    WeeklyPattern: [("title", "title", str), ("day", "day", parse_weekday),
                    ("start HH:MM", "start_time", parse_time), ("end HH:MM", "end_time", parse_time)],
    DatedBlock: [("title", "title", str), ("date YYYY-MM-DD", "date", parse_date),
                 ("start HH:MM", "start_time", parse_time), ("end HH:MM", "end_time", parse_time)],
    ExtractedTask: [("title", "title", str), ("due YYYY-MM-DD [HH:MM]", "date", parse_due),
                    ("hours", "duration_slots", parse_hours), ("priority 1-5", "priority", parse_rating),
                    ("difficulty 1-5", "difficulty", parse_rating)],
}


def _current(item, key: str):
    value = getattr(item, key)
    if key == "date" and isinstance(item, ExtractedTask):
        return item.due_label()
    return f"{slots_to_hours(value):g}" if key == "duration_slots" else value


def _describe(kind: str, item) -> str:
    if kind == "Task":
        return describe_task(item)
    when = item.day if kind == "Weekly" else item.date
    return f"{item.title} -- {when} {item.start_time}-{item.end_time}"


def _edit_item(item, ask, show, session_cap: int | None):
    """Ask for each field (Enter keeps the current value). Bad input shows why and re-asks."""
    while True:
        try:
            changes = {}
            for label, key, parse in _EDIT_FIELDS[type(item)]:
                raw = ask(f"  {label} [{_current(item, key)}]: ").strip()
                if raw and parse is parse_due:  #   -- one answer sets the date and the (optional) time
                    changes["date"], changes["due_time"] = parse(raw)
                    if item.due_time and changes["due_time"] is None:
                        show(f"  Due time {item.due_time} removed: now due at the end of that day.")
                elif raw:
                    changes[key] = parse(raw)
            if isinstance(item, ExtractedTask) and session_cap:  #   -- only long tasks can be split
                if changes.get("duration_slots", item.duration_slots) > session_cap:
                    cap_h = slots_to_hours(session_cap)
                    changes["splittable"] = confirm(
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