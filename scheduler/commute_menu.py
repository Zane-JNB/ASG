import re
from datetime import date

from scheduler.db import COMMUTES, skip_commute_date, transaction
from scheduler.models import Commute
from scheduler.prompts import ask_until, confirm, parse_whole, pick
from scheduler.units import WEEKDAYS, parse_date, parse_time, parse_weekday, weekday_name


def _minutes(s: str) -> int:
    return parse_whole(s, 1, 720)


def _weekdays(s: str) -> list[str]:
    names = [parse_weekday(t) for t in re.split(r"[,\s]+", s.strip()) if t]
    if not names:
        raise ValueError("enter at least one weekday")
    return sorted(set(names), key=WEEKDAYS.index)


def _describe(c: Commute) -> str:
    if not c.recurring:
        return f"{c.title}: {c.date} {c.start_time}, {c.length_minutes} min (one-time)"
    text = f"{c.title}: every {c.weekday} {c.start_time}, {c.length_minutes} min"
    if c.end_date:
        text += f", ends after {c.end_date}"
    if c.skip_dates:
        text += f", skipping {', '.join(sorted(c.skip_dates))}"
    return text


def _sorted(conn, student_id: int):
    def key(item):
        c = item[1]
        return (not c.recurring, WEEKDAYS.index(c.weekday) if c.recurring else 0, c.date or "", c.start_time)
    return sorted(COMMUTES.get(conn, student_id), key=key)


def _show_list(items, show) -> None:
    if not items:
        show("No commutes saved.")
    for n, (_, c) in enumerate(items, 1):
        show(f"{n}. {_describe(c)}")


def _add(conn, student_id: int, ask, show, today: date) -> None:
    def not_past(s: str) -> str:
        day = parse_date(s)
        if date.fromisoformat(day) < today:
            raise ValueError("that date has already passed")
        return day

    title = ask("  Name (Enter for 'Commute'): ").strip() or "Commute"
    start = ask_until(ask, show, "Start time (HH:MM, 24-hour, Enter to cancel)", parse_time)
    if start is None:
        show("Cancelled -- nothing saved.")
        return
    length = ask_until(ask, show, "Length in minutes, including any waiting time", _minutes)
    if length is None:
        show("Cancelled -- nothing saved.")
        return
    base = dict(title=title, start_time=start, length_minutes=length)
    if confirm(ask, "  Repeat every week?", False):
        days = ask_until(ask, show, "Weekdays, e.g. Mon,Wed,Fri (Enter to cancel)", _weekdays)
        if days is None:
            show("Cancelled -- nothing saved.")
            return
        made = [Commute(**base, recurring=True, weekday=d) for d in days]
    else:
        when = ask_until(ask, show, f"Date [{today.isoformat()}]", not_past, default=today.isoformat())
        made = [Commute(**base, date=when)]
    with transaction(conn):  # every weekday of a recurring commute, or none
        for c in made:
            COMMUTES.add(conn, student_id, c)
    show(f"Added {len(made)} commute(s):")
    for c in made:
        show(f"  {_describe(c)}")


def _skip(conn, student_id: int, ask, show) -> None:
    items = _sorted(conn, student_id)
    _show_list(items, show)
    if not items:
        return
    picked = pick(ask, show, items, "Number to skip or end (Enter to cancel): ")
    if picked is None:
        return
    cid, c = picked
    if not c.recurring:  # a one-time commute has nothing to come back to, so skipping = deleting
        if confirm(ask, f"  '{c.title}' is one-time, so skipping it deletes it. Delete?", False):
            COMMUTES.delete(conn, student_id, cid)
            show("Deleted.")
        else:
            show("Cancelled -- nothing changed.")
        return
    mode = ask("  Skip [o]ne date, or [e]nd it after a date? (Enter to cancel): ").strip().lower()
    if mode == "o":
        day = ask_until(ask, show, f"Date to skip (YYYY-MM-DD, a {c.weekday})", parse_date)
        if day is None:
            show("Cancelled -- nothing changed.")
            return
        if weekday_name(date.fromisoformat(day)) != c.weekday:
            show(f"  {day} is not a {c.weekday}, so there is nothing to skip.")
            return
        skip_commute_date(conn, student_id, cid, day)
        show(f"Skipped '{c.title}' on {day}.")
    elif mode == "e":
        last = ask_until(ask, show, "Last day it should still happen (YYYY-MM-DD)", parse_date)
        if last is None:
            show("Cancelled -- nothing changed.")
            return
        COMMUTES.update(conn, student_id, cid, c.model_copy(update={"end_date": last}))
        show(f"'{c.title}' will stop after {last}.")
    elif mode:
        show("Choose o or e.")


def run_commute_menu(conn, student_id, ask=input, show=print, today: date | None = None) -> None:
    today = today or date.today()
    while True:
        choice = ask("Commutes: [a]dd  [l]ist  [s]kip/end  [d]elete  [b]ack: ").strip().lower()
        if choice == "b":
            return
        if choice == "a":
            _add(conn, student_id, ask, show, today)
        elif choice == "l":
            _show_list(_sorted(conn, student_id), show)
        elif choice == "s":
            _skip(conn, student_id, ask, show)
        elif choice == "d":
            items = _sorted(conn, student_id)
            _show_list(items, show)
            if not items:
                continue
            picked = pick(ask, show, items, "Number to delete (Enter to cancel): ")
            if picked:
                COMMUTES.delete(conn, student_id, picked[0])
                show("Deleted.")
        else:
            show("Choose a, l, s, d or b.")