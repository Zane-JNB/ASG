from collections.abc import Callable
from typing import Any, NamedTuple

from scheduler.db import load_approval_mode, load_settings, load_tiers
from scheduler.preference_policy import POLICY, ApprovalMode, Tier
from scheduler.preferences import (Actor, PendingChange, PreferenceError, change_tier, check_may_edit,
                                   set_approval_mode, set_values)
from scheduler.prompts import ask_until, parse_hours, parse_rating, parse_whole, pick, run_choices
from scheduler.units import MINUTES_PER_SLOT, SLOTS_PER_DAY, parse_time, slot_to_time, slots_to_hours, time_to_slot

_CANCEL = object()  # Enter = cancel; separate sentinel because "none" is a legal value (None)
_TIER_WORDS = {Tier.USER: "set by you", Tier.MODEL_LEARNED: "learned automatically", Tier.LOCKED: "protected"}
_MODE_WORDS = {ApprovalMode.ASK: "ask for your approval first", ApprovalMode.AUTO: "be applied automatically"}


# ---------- how each editable setting is typed and shown ----------

def _minutes(s: str) -> int:
    """Whole minutes in 15-minute steps -> slots."""
    n = parse_whole(s, 0, SLOTS_PER_DAY * MINUTES_PER_SLOT)
    if n % MINUTES_PER_SLOT:
        raise ValueError(f"enter minutes in steps of {MINUTES_PER_SLOT} (0 allowed)")
    return n // MINUTES_PER_SLOT


def _bed_slot(s: str) -> int:
    """A bedtime on the 2-day bed timeline: times before noon mean after midnight."""
    slot = time_to_slot(parse_time(s))
    return slot + SLOTS_PER_DAY if slot < SLOTS_PER_DAY // 2 else slot


def _yes_no(s: str) -> bool:
    if s.lower() in ("y", "yes", "on"):
        return True
    if s.lower() in ("n", "no", "off"):
        return False
    raise ValueError("answer yes or no")


def _rating_or_none(s: str) -> int | None:
    return None if s.lower() == "none" else parse_rating(s)


class _Field(NamedTuple):
    parse: Callable[[str], Any]
    fmt: Callable[[Any], str]
    hint: str


_HOURS = (parse_hours, lambda v: f"{slots_to_hours(v):g} h")
_MINUTES = (_minutes, lambda v: f"{v * MINUTES_PER_SLOT} min")
_BEDTIME = (_bed_slot, lambda v: slot_to_time(v % SLOTS_PER_DAY) + (" (after midnight)" if v >= SLOTS_PER_DAY else ""))
_RATING = (_rating_or_none, lambda v: "any" if v is None else str(v))

_UI = {  # Every user_editable POLICY field must be here (tested).
    "buffer_slots": _Field(*_MINUTES, "minutes, e.g. 15"),
    "default_max_session_slots": _Field(*_HOURS, "hours, e.g. 2"),
    "default_sleep_length_slots": _Field(*_HOURS, "hours, e.g. 8"),
    "default_sleep_min_slots": _Field(*_HOURS, "hours, e.g. 6"),
    "default_preferred_bed": _Field(*_BEDTIME, "HH:MM, e.g. 23:00"),
    "default_earliest_bed": _Field(*_BEDTIME, "HH:MM, e.g. 21:00"),
    "default_latest_bed": _Field(*_BEDTIME, "HH:MM, e.g. 01:00"),
    "wake_buffer_slots": _Field(*_MINUTES, "minutes, e.g. 60"),
    "reminders_enabled": _Field(_yes_no, lambda v: "on" if v else "off", "yes/no"),
    "reminder_min_difficulty": _Field(*_RATING, "1-5, or 'none'"),
    "reminder_min_priority": _Field(*_RATING, "1-5, or 'none'"),
}


def describe_pending(p: PendingChange) -> str:
    if p.field in _UI:
        fmt = _UI[p.field].fmt
        return f"{p.label}: {fmt(p.old)} -> {fmt(p.new)}"
    return f"{p.label}: {p.direction} slightly"


# ---------- the menu ----------

def _approval(conn, student_id: int, ask, show) -> None:
    show(f"Learned changes currently {_MODE_WORDS[load_approval_mode(conn, student_id)]}.")
    raw = ask("Switch to: ask (approve each change) / auto (apply after repeated evidence); Enter to keep: ").strip().lower()
    if not raw:
        return
    if raw not in tuple(ApprovalMode):
        show("Type ask or auto.")
        return
    changed = set_approval_mode(conn, student_id, raw, Actor.USER)
    show(f"Learned changes will now {_MODE_WORDS[raw]}." if changed else "No change.")


def _user_facing():
    return [n for n, p in POLICY.items() if p.user_editable]


def show_settings(conn, student_id: int, show) -> list[str]:
    """Number and show every setting the student can see; returns their names in that order."""
    settings, tiers, fields = load_settings(conn, student_id), load_tiers(conn, student_id), _user_facing()
    for n, name in enumerate(fields, 1):
        show(f"{n}. {POLICY[name].label}: {_UI[name].fmt(getattr(settings, name))}  [{_TIER_WORDS[tiers[name]]}]")
    return fields


def show_internal(conn, student_id: int, show) -> None:
    tiers = load_tiers(conn, student_id)
    show("Managed by the app (you can't edit these):")
    for name, p in POLICY.items():
        if not p.user_editable:
            show(f"  - {p.label}  [{_TIER_WORDS[tiers[name]]}]")


def _edit(conn, student_id: int, name: str, ask, show) -> None:
    try:  # same friendly messages as the API, and we don't ask for a value we'd then reject
        check_may_edit(name, load_tiers(conn, student_id), Actor.USER)
    except PreferenceError as e:
        show(str(e))
        return
    ui = _UI[name]
    value = ask_until(ask, show, f"New value for {POLICY[name].label} ({ui.hint}; Enter to cancel)", ui.parse,
                      default=_CANCEL)
    if value is _CANCEL:
        return
    before = getattr(load_settings(conn, student_id), name)
    try:
        after = getattr(set_values(conn, student_id, {name: value}, Actor.USER), name)
    except PreferenceError as e:
        show(str(e))
        return
    show(f"Saved: {POLICY[name].label} is now {ui.fmt(after)}." if after != before else "No change.")


def _switch(conn, student_id: int, name: str, new_tier: Tier, show) -> None:
    label = POLICY[name].label
    try:
        changed = change_tier(conn, student_id, name, new_tier, Actor.USER)
    except PreferenceError as e:
        show(str(e))
        return
    value = _UI[name].fmt(getattr(load_settings(conn, student_id), name))
    if not changed:
        show(f"'{label}' is already {_TIER_WORDS[new_tier]}.")
    elif new_tier == Tier.USER:
        show(f"You now control '{label}' (kept at {value}). The app will stop adjusting it.")
    else:
        show(f"'{label}' is automatic again, starting from {value}.")


def run_settings_menu(conn, student_id: int, ask=input, show=print) -> None:
    fields = show_settings(conn, student_id, show)

    def on_picked(act: Callable[[str], None]) -> Callable[[], None]:
        """List the settings, let the student pick one by number, then act on it."""
        def pick_then_act() -> None:
            show_settings(conn, student_id, show)
            name = pick(ask, show, fields, "Number (Enter to cancel): ")
            if name is not None:
                act(name)
        return pick_then_act

    run_choices(ask, show, "Settings", {
        "l": ("[l]ist", lambda: show_settings(conn, student_id, show)),
        "e": ("[e]dit", on_picked(lambda name: _edit(conn, student_id, name, ask, show))),
        "m": ("[m]anual (take control)", on_picked(lambda name: _switch(conn, student_id, name, Tier.USER, show))),
        "a": ("[a]utomatic (let the app learn)",
              on_picked(lambda name: _switch(conn, student_id, name, Tier.MODEL_LEARNED, show))),
        "i": ("[i]nternal", lambda: show_internal(conn, student_id, show)),
        "o": ("[o] approval mode", lambda: _approval(conn, student_id, ask, show)),
    }, done=("q", "[q]uit"))
