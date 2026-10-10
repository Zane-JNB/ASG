"""The app's time axis: 15-minute slots, clock times, dates, weekdays, and how hours are shown.
Every other module uses these instead of re-parsing 'HH:MM' or re-deriving hours from slots."""
from datetime import date
from typing import Literal, get_args

MINUTES_PER_SLOT = 15
SLOTS_PER_DAY = 24 * 60 // MINUTES_PER_SLOT  # 96 slots in a day

Weekday = Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAYS: tuple[Weekday, ...] = get_args(Weekday)  # date.weekday() order


def parse_time(value: str, end: bool = False) -> str:
    """'HH:MM', 24-hour, returned zero-padded ('9:30' -> '09:30') so times sort correctly.
    An end time may be '24:00' (midnight at the end of the day)."""
    h, sep, m = value.partition(":")
    if not (sep and h.isascii() and h.isdigit() and m.isascii() and m.isdigit()
            and len(h) <= 2 and len(m) == 2):
        raise ValueError(f"'{value}' is not a time like 09:30")
    normalized = f"{int(h):02d}:{m}"
    if end and normalized == "24:00":
        return normalized
    if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
        raise ValueError(f"'{value}' is not a real time" + (" (latest end is 24:00)" if end else ""))
    return normalized


def parse_due_time(value: str) -> str:
    """A task's due time ('24:00' allowed), rounded down to its 15-minute slot so the shown
    deadline, the solver deadline and the overdue check all agree ('09:10' -> '09:00')."""
    return slot_to_time(time_to_slot(parse_time(value, end=True)))


def parse_date(value: str) -> str:
    """A real date, returned as 'YYYY-MM-DD' so dates sort and compare correctly."""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError(f"'{value}' is not a real date like 2026-10-05") from None


def parse_weekday(value: str) -> Weekday:
    """'monday' or 'Mon' -> 'Mon'."""
    day = value.strip().capitalize()[:3]
    if day not in WEEKDAYS:
        raise ValueError(f"day must be one of {', '.join(WEEKDAYS)}")
    return day


def weekday_name(d: date) -> Weekday:
    """'Mon'..'Sun' for a real date."""
    return WEEKDAYS[d.weekday()]


def time_to_minutes(hhmm: str) -> int:
    """'09:30' -> 570, minutes since midnight."""
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def time_to_slot(hhmm: str) -> int:
    """'09:30' -> 38 (rounded down to its slot)."""
    return time_to_minutes(hhmm) // MINUTES_PER_SLOT


def slot_to_time(slot: int) -> str:
    """38 -> '09:30'"""
    minutes = slot * MINUTES_PER_SLOT
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def clock_range(start_slot: int, end_slot: int) -> str:
    """'09:00-10:30'. An end past midnight (sleep, late blocks) shows as the next day's clock time."""
    return f"{slot_to_time(start_slot)}-{slot_to_time(end_slot % SLOTS_PER_DAY)}"


def slots_to_hours(slots: int) -> float:
    """6 -> 1.5"""
    return slots * MINUTES_PER_SLOT / 60


def format_hours(slots: int) -> str:
    """7 -> '1h 45m', 4 -> '1h', 2 -> '30m', 0 -> '0m'."""
    h, m = divmod(slots * MINUTES_PER_SLOT, 60)
    return " ".join(part for part in (f"{h}h" if h else "", f"{m}m" if m else "") if part) or "0m"
