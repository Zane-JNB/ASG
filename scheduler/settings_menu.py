from scheduler.menu_input import ask_until, pick                      
from scheduler.models import MINUTES_PER_SLOT, SLOTS_PER_DAY, slot_to_time, time_to_slot
from scheduler.preference_policy import POLICY, Tier
from scheduler.preferences import (APPROVAL_ASK, APPROVAL_AUTO, Actor, PreferenceError, _check_may_edit,  
                                   change_tier, get_approval_mode, get_effective, get_ownership,
                                   set_approval_mode, user_edit)
from scheduler.review import _hours

_CANCEL = object()  # Enter = cancel; separate sentinel because "none" is a legal value (None)
_TIER_WORDS = {Tier.USER: "set by you", Tier.MODEL_LEARNED: "learned automatically", Tier.LOCKED: "protected"}
_MODE_WORDS = {APPROVAL_ASK: "ask for your approval first", APPROVAL_AUTO: "be applied automatically"}

def describe_pending(p) -> str:   
    if p.field in _UI:
        fmt = _UI[p.field][1]
        return f"{p.label}: {fmt(p.old)} -> {fmt(p.new)}"
    return f"{p.label}: {p.direction} slightly"

def _approval(conn, student_id, ask, show) -> None:  
    show(f"Learned changes currently {_MODE_WORDS[get_approval_mode(conn, student_id)]}.")
    raw = ask("Switch to: ask (approve each change) / auto (apply after repeated evidence); Enter to keep: ").strip().lower()
    if not raw:
        return
    if raw not in (APPROVAL_ASK, APPROVAL_AUTO):
        show("Type ask or auto."); return
    changed = set_approval_mode(conn, student_id, raw, Actor.USER)
    show(f"Learned changes will now {_MODE_WORDS[raw]}." if changed else "No change.")

def _num(conv, s, what):                                            
    try: return conv(s)
    except ValueError: raise ValueError(f"enter {what}") from None

def _hrs(s):                                                        
    _num(float, s, "a number of hours, e.g. 2 or 1.5")
    return _hours(s)

def _minutes(s):                                                    
    n = _num(int, s, "a whole number of minutes")
    if n < 0 or n % MINUTES_PER_SLOT:
        raise ValueError(f"enter minutes in steps of {MINUTES_PER_SLOT} (0 allowed)")
    return n // MINUTES_PER_SLOT

def _clock(s):  #  times before noon mean "after midnight" on the 2-day bed timeline
    try:
        h, m = (int(x) for x in s.strip().split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59): raise ValueError
    except ValueError:
        raise ValueError("use HH:MM, 24-hour (e.g. 23:00)") from None
    slot = time_to_slot(f"{h}:{m}")
    return slot + SLOTS_PER_DAY if slot < SLOTS_PER_DAY // 2 else slot

def _bool(s):                                                       
    if s.lower() in ("y", "yes", "on"): return True
    if s.lower() in ("n", "no", "off"): return False
    raise ValueError("answer yes or no")

def _rating(s):                                                     
    if s.lower() == "none": return None
    n = _num(int, s, "1-5, or 'none'")
    if not 1 <= n <= 5: raise ValueError("enter 1-5, or 'none'")
    return n

def _fmt_hours(v): return f"{v * MINUTES_PER_SLOT / 60:g} h"
def _fmt_minutes(v): return f"{v * MINUTES_PER_SLOT} min"
def _fmt_clock(v): return slot_to_time(v % SLOTS_PER_DAY) + (" (after midnight)" if v >= SLOTS_PER_DAY else "")
def _fmt_bool(v): return "on" if v else "off"
def _fmt_rating(v): return "any" if v is None else str(v)

_UI = {  # field -> (parse, format, hint). Every user_editable POLICY field must be here (tested).
    "buffer_slots": (_minutes, _fmt_minutes, "minutes, e.g. 15"),
    "default_max_session_slots": (_hrs, _fmt_hours, "hours, e.g. 2"),
    "default_sleep_length_slots": (_hrs, _fmt_hours, "hours, e.g. 8"),
    "default_sleep_min_slots": (_hrs, _fmt_hours, "hours, e.g. 6"),
    "default_preferred_bed": (_clock, _fmt_clock, "HH:MM, e.g. 23:00"),
    "default_earliest_bed": (_clock, _fmt_clock, "HH:MM, e.g. 21:00"),
    "default_latest_bed": (_clock, _fmt_clock, "HH:MM, e.g. 01:00"),
    "reminders_enabled": (_bool, _fmt_bool, "yes/no"),
    "reminder_min_difficulty": (_rating, _fmt_rating, "1-5, or 'none'"),
    "reminder_min_priority": (_rating, _fmt_rating, "1-5, or 'none'"),
}

def _user_facing():   
    return [n for n, p in POLICY.items() if p.user_editable]

def show_settings(conn, student_id, show):  
    settings, tiers, fields = get_effective(conn, student_id), get_ownership(conn, student_id), _user_facing()
    for n, name in enumerate(fields, 1):
        show(f"{n}. {POLICY[name].label}: {_UI[name][1](getattr(settings, name))}  [{_TIER_WORDS[tiers[name]]}]")
    return fields

def show_internal(conn, student_id, show):   
    tiers = get_ownership(conn, student_id)
    show("Managed by the app (you can't edit these):")
    for name, p in POLICY.items():
        if not p.user_editable:
            show(f"  - {p.label}  [{_TIER_WORDS[tiers[name]]}]")

def _edit(conn, student_id, name, ask, show):  
    try:  # same friendly messages as the API, and we don't ask for a value we'd then reject
        _check_may_edit(name, get_ownership(conn, student_id), Actor.USER)
    except PreferenceError as e:
        show(str(e)); return
    parse, fmt, hint = _UI[name]
    value = ask_until(ask, show, f"New value for {POLICY[name].label} ({hint}; Enter to cancel)", parse, default=_CANCEL)
    if value is _CANCEL:
        return
    before = getattr(get_effective(conn, student_id), name)
    try:
        after = getattr(user_edit(conn, student_id, name, value), name)
    except PreferenceError as e:
        show(str(e)); return
    show(f"Saved: {POLICY[name].label} is now {fmt(after)}." if after != before else "No change.")

def _switch(conn, student_id, name, new_tier, show):  
    label = POLICY[name].label
    try:
        changed = change_tier(conn, student_id, name, new_tier, Actor.USER)
    except PreferenceError as e:
        show(str(e)); return
    value = _UI[name][1](getattr(get_effective(conn, student_id), name))
    if not changed:
        show(f"'{label}' is already {_TIER_WORDS[new_tier]}.")
    elif new_tier == Tier.USER:
        show(f"You now control '{label}' (kept at {value}). The app will stop adjusting it.")
    else:
        show(f"'{label}' is automatic again, starting from {value}.")

def run_settings_menu(conn, student_id, ask=input, show=print):  
    fields = show_settings(conn, student_id, show)
    while True:
        choice = ask("Settings: [l]ist  [e]dit  [m]anual (take control)  [a]utomatic (let the app learn)  [i]nternal  [o] approval mode  [q]uit: ").strip().lower()
        if choice == "q": return
        if choice == "l": show_settings(conn, student_id, show)
        elif choice == "i": show_internal(conn, student_id, show)
        elif choice == "o":  
            _approval(conn, student_id, ask, show)
        elif choice in ("e", "m", "a"):
            show_settings(conn, student_id, show)
            item = pick(ask, show, fields, "Number (Enter to cancel): ")
            if item is None: continue
            if choice == "e": _edit(conn, student_id, item, ask, show)
            else: _switch(conn, student_id, item, Tier.USER if choice == "m" else Tier.MODEL_LEARNED, show)
        elif choice:
            show("Choose l, e, m, a, i, o or q.")