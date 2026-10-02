from scheduler.drop_review import _h  # NEW
from scheduler.manual_cuts import NEW, CutState  # NEW
from scheduler.models import MINUTES_PER_SLOT
from scheduler.review import _hours  # NEW


def ask_mode(ask=input, show=print) -> str | None:  # NEW  "m" / "s" / "a" / None (cancel)
    while True:
        raw = ask("How do you want to make room? [m]anual  [s]emi-automatic  "
                  "[a]utomatic (Enter to cancel): ").strip().lower()
        if raw == "":
            return None
        if raw in ("m", "s", "a"):
            return raw
        show("Choose m, s or a.")


def _show_list(state: CutState, titles: dict, order: list, show) -> None:  # NEW
    show("Your tasks (you can cut any of them, including the new one):")
    for n, i in enumerate(order, 1):
        left = state.remaining(i)
        if left == 0:
            what = "removed"
        else:
            k = len(state.sizes_fn(i, left))
            what = f"{_h(left)}, {k} session{'s' if k != 1 else ''}"
        was = f" (was {_h(state.durations[i])})" if state.lost.get(i) else ""
        show(f"  {n}. {titles[i]}{' (new)' if i == NEW else ''} -- {what}{was}")


def _hours_or_error(text: str) -> int:  # NEW
    try:
        return _hours(text)
    except ValueError:
        raise ValueError("enter hours as a number, like 1.5 (at least 0.25)") from None


def _edit_task(state: CutState, i: int, ask, show) -> bool:  # NEW  True if something changed
    left = state.remaining(i)
    if left == 0:
        show("  That task is already fully cut. Undo to bring it back.")
        return False
    sessions = len(state.sizes_fn(i, left))
    options = ([] if i == NEW else ["[d]rop it"]) + (["[s]essions"] if sessions > 1 else []) + ["[t]ime"]
    raw = ask("  " + "  ".join(options) + "  (Enter to go back): ").strip().lower()
    try:
        if raw == "":
            return False
        if raw == "d" and i != NEW:
            state.drop(i)
        elif raw == "s" and sessions > 1:
            text = ask(f"  Cut how many of its {sessions} sessions (1-{sessions - 1})? ").strip()
            if not text.isdigit() or not 1 <= int(text) <= sessions - 1:
                raise ValueError(f"enter a whole number from 1 to {sessions - 1}")
            state.cut_chunks(i, int(text))
        elif raw == "t":
            state.reduce(i, _hours_or_error(ask(f"  Reduce by how many hours (up to {(left - 1) * MINUTES_PER_SLOT / 60:g}, e.g. 1.5)? ").strip()))
        else:
            show("  Choose one of the options shown.")
            return False
    except ValueError as e:
        show(f"  Not done: {e}")
        return False
    return True


def run_manual_edit(state: CutState, titles: dict, fits, ask=input, show=print,
                    must_add: bool = False) -> tuple[str, object]:  # NEW
    """fits(lost) -> a verified proposal if everything fits after those cuts, else None.
    Returns ("save", proposal), ("dont_add", None) or ("cancel", None). Nothing is saved here."""
    order = sorted(i for i in state.durations if i != NEW) + [NEW]
    result = None
    while True:
        _show_list(state, titles, order, show)
        if state.touched:
            if result:
                show("Everything fits now.")
                if result.sleep_sacrificed_slots:
                    show(f"  !! Sleep: {_h(result.sleep_sacrificed_slots)} below target")
                for f in result.flags:
                    show(f"  !! {f}")
            else:
                show("Still doesn't fit -- keep cutting, undo, or cancel.")
        extras = (["[s]ave"] if result else []) + (["[u]ndo"] if state.touched else [])
        if not must_add:
            extras.append("[n] don't add the new task" + (" (discards your cuts)" if state.touched else ""))
        raw = ask(f"Pick 1-{len(order)} to cut  " + "  ".join(extras) + "  (Enter to cancel): ").strip().lower()

        if raw == "":
            if state.touched and ask("Discard your cuts and cancel? [y/N]: ").strip().lower() != "y":
                continue
            return "cancel", None
        if raw == "s":
            if result:
                return "save", result
            show("It doesn't fit yet, so there is nothing to save.")
        elif raw == "u":
            if state.undo():
                result = fits(state.lost) if state.touched else None
                show("Undid the last edit.")
            else:
                show("Nothing to undo.")
        elif raw == "n" and not must_add:
            return "dont_add", None
        elif raw.isdigit() and 1 <= int(raw) <= len(order):
            if _edit_task(state, order[int(raw) - 1], ask, show):
                result = fits(state.lost)
        else:
            show(f"Enter a number from 1 to {len(order)}" + (", or one of the letters shown." if extras else "."))