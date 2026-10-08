import re
from datetime import date

from scheduler.calendar_utils import weekday_name
from scheduler.db import add_commute, delete_commute, get_commutes, skip_commute_date, update_commute
from scheduler.menu_input import ask_until, pick
from scheduler.models import Commute
from scheduler.review import _confirm, _date, _day

_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

def _clock(s: str) -> str:
    try:
        h, m = (int(x) for x in s.strip().split(":"))
    except ValueError:
        raise ValueError("use HH:MM, 24-hour (e.g. 07:45)") from None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("use HH:MM, 24-hour (e.g. 07:45)")
    return f"{h:02d}:{m:02d}"

def _minutes(s: str) -> int:
    try:
        n = int(s)
    except ValueError:
        raise ValueError("enter a whole number of minutes") from None
    if not 1 <= n <= 720:
        raise ValueError("enter between 1 and 720 minutes")
    return n

def _weekdays(s: str) -> list[str]:
    names = [_day(t) for t in re.split(r"[,\s]+", s.strip()) if t]
    if not names:
        raise ValueError("enter at least one weekday")
    return sorted(set(names), key=_DAYS.index)

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
        return (not c.recurring, _DAYS.index(c.weekday) if c.recurring else 0, c.date or "", c.start_time)
    return sorted(get_commutes(conn, student_id), key=key)

def _show_list(items, show) -> None:
    if not items:
        show("No commutes saved.")
    for n, (_, c) in enumerate(items, 1):
        show(f"{n}. {_describe(c)}")

def _add(conn, student_id: int, ask, show, today: date) -> None:
    def not_past(s: str) -> str:
        if date.fromisoformat(_date(s)) < today:
            raise ValueError("that date has already passed")
        return _date(s)

    title = ask("  Name (Enter for 'Commute'): ").strip() or "Commute"
    start = ask_until(ask, show, "Start time (HH:MM, 24-hour, Enter to cancel)", _clock)
    if start is None:
        return show("Cancelled -- nothing saved.")
    length = ask_until(ask, show, "Length in minutes, including any waiting time", _minutes)
    if length is None:
        return show("Cancelled -- nothing saved.")
    base = dict(title=title, start_time=start, length_minutes=length)
    if _confirm(ask, "  Repeat every week?", False):
        days = ask_until(ask, show, "Weekdays, e.g. Mon,Wed,Fri (Enter to cancel)", _weekdays)
        if days is None:
            return show("Cancelled -- nothing saved.")
        made = [Commute(**base, recurring=True, weekday=d) for d in days]
    else:
        when = ask_until(ask, show, f"Date [{today.isoformat()}]", not_past, default=today.isoformat())
        made = [Commute(**base, date=when)]
    for c in made:
        add_commute(conn, student_id, c)
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
        if _confirm(ask, f"  '{c.title}' is one-time, so skipping it deletes it. Delete?", False):
            delete_commute(conn, student_id, cid)
            show("Deleted.")
        else:
            show("Cancelled -- nothing changed.")
        return
    mode = ask("  Skip [o]ne date, or [e]nd it after a date? (Enter to cancel): ").strip().lower()
    if mode == "o":
        day = ask_until(ask, show, f"Date to skip (YYYY-MM-DD, a {c.weekday})", _date)
        if day is None:
            return show("Cancelled -- nothing changed.")
        if weekday_name(date.fromisoformat(day)) != c.weekday:
            return show(f"  {day} is not a {c.weekday}, so there is nothing to skip.")
        skip_commute_date(conn, student_id, cid, day)
        show(f"Skipped '{c.title}' on {day}.")
    elif mode == "e":
        last = ask_until(ask, show, "Last day it should still happen (YYYY-MM-DD)", _date)
        if last is None:
            return show("Cancelled -- nothing changed.")
        update_commute(conn, student_id, cid, c.model_copy(update={"end_date": last}))
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
                delete_commute(conn, student_id, picked[0])
                show("Deleted.")
        else:
            show("Choose a, l, s, d or b.")