"""Review of an extraction before it is saved: list everything found, then edit or delete by number."""
from collections.abc import Callable
from typing import Any, NamedTuple

from pydantic import ValidationError

from scheduler.models import DatedBlock, ExtractedTask, ExtractionResult, TimeRange, WeeklyPattern
from scheduler.prompts import confirm, describe_task, parse_due, parse_hours, parse_rating
from scheduler.units import parse_date, parse_time, parse_weekday, slots_to_hours

Item = WeeklyPattern | DatedBlock | ExtractedTask
_KIND = {WeeklyPattern: "Weekly", DatedBlock: "Session", ExtractedTask: "Task"}


class _Field(NamedTuple):
    """One editable answer: its prompt label, the current value as shown, and the parser that
    turns a typed answer into model changes."""
    label: str
    shown: Callable[[Any], object]
    parse: Callable[[str], dict]


def _plain(label: str, key: str, parse: Callable[[str], object]) -> _Field:
    return _Field(label, lambda item: getattr(item, key), lambda s: {key: parse(s)})


_TITLE = _plain("title", "title", str)
_TIMES = [_plain("start HH:MM", "start_time", parse_time),
          _plain("end HH:MM", "end_time", lambda s: parse_time(s, end=True))]  # an end may be 24:00
_FIELDS: dict[type, list[_Field]] = {
    WeeklyPattern: [_TITLE, _plain("day", "day", parse_weekday), *_TIMES],
    DatedBlock: [_TITLE, _plain("date YYYY-MM-DD", "date", parse_date), *_TIMES],
    ExtractedTask: [
        _TITLE,
        _Field("due YYYY-MM-DD [HH:MM]", ExtractedTask.due_label,  # one answer sets the date and the time
               lambda s: dict(zip(("date", "due_time"), parse_due(s)))),
        _Field("hours", lambda t: f"{slots_to_hours(t.duration_slots):g}",
               lambda s: {"duration_slots": parse_hours(s)}),
        _plain("priority 1-5", "priority", parse_rating),
        _plain("difficulty 1-5", "difficulty", parse_rating),
    ],
}


def _describe(item: Item) -> str:
    if isinstance(item, ExtractedTask):
        return describe_task(item)
    return f"{item.title} -- {item.when} {item.start_time}-{item.end_time}"


def _edit_item(item: Item, ask, show, session_cap: int | None) -> Item:
    """Ask for each field (Enter keeps the current value). Bad input shows why and re-asks."""
    while True:
        try:
            changes = {}
            for field in _FIELDS[type(item)]:
                raw = ask(f"  {field.label} [{field.shown(item)}]: ").strip()
                if raw:
                    changes |= field.parse(raw)
                if raw and isinstance(item, ExtractedTask) and item.due_time and changes.get("due_time", "") is None:
                    show(f"  Due time {item.due_time} removed: now due at the end of that day.")
            if isinstance(item, ExtractedTask) and session_cap:  # only long tasks can be split
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


def _overlap_notes(items: list[Item]) -> list[str]:
    """Numbered items that clash: weekly ones on the same weekday, sessions on the same date."""
    return [f"{i + 1} and {j + 1}" for i, a in enumerate(items) for j in range(i + 1, len(items))
            if isinstance(a, TimeRange) and a.clashes(items[j])]


def _line(n: int, item: Item) -> str:
    return f"{n}. [{_KIND[type(item)]}] {_describe(item)}"


def review_extraction(result: ExtractionResult, ask=input, show=print, session_cap: int | None = None) -> ExtractionResult:
    """Show everything found, then ONE prompt: Enter accepts all, or pick numbers to edit/delete."""
    items: list[Item | None] = [*result.weekly_patterns, *result.dated_blocks, *result.tasks]
    if not items:
        return ExtractionResult()

    show("Found:")
    for n, item in enumerate(items, 1):
        show(_line(n, item))
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
        item = items[n - 1]
        show(_line(n, item))
        while True:
            action = ask("  [e]dit, [d]elete, or Enter to leave as is: ").strip().lower()
            if action in ("", "e", "d"):
                break
        if action == "d":
            items[n - 1] = None
        elif action == "e":
            items[n - 1] = _edit_item(item, ask, show, session_cap)
    return ExtractionResult(
        weekly_patterns=[i for i in items if isinstance(i, WeeklyPattern)],
        dated_blocks=[i for i in items if isinstance(i, DatedBlock)],
        tasks=[i for i in items if isinstance(i, ExtractedTask)],
    )