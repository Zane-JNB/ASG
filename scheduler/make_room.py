"""The "make room" screens for a new task that doesn't fit: the mode prompt, the ranked options
(semi-automatic and automatic), and the manual editor with its undo-able cuts. Nothing is saved here."""
from scheduler.dropping import NEW_TASK, dont_add_unverified
from scheduler.models import DropAction, DropProposal, DropReport, DynamicTask
from scheduler.review import _hours
from scheduler.units import format_hours, slots_to_hours


def ask_mode(ask=input, show=print) -> str | None:
    while True:
        raw = ask("How do you want to make room? [m]anual  [s]emi-automatic  "
                  "[a]utomatic (Enter to cancel): ").strip().lower()
        if raw == "":
            return None
        if raw in ("m", "s", "a"):
            return raw
        show("Choose m, s or a.")


# ---------- semi-automatic and automatic: the ranked search's options ----------

def _action_line(a: DropAction) -> str:
    if a.shrink:
        left = a.total_chunks - a.chunks_cut
        sessions = "one block" if left == 1 else f"{left} sessions"
        what = (f"Shorten '{a.title}' by {format_hours(a.slots_lost)} ({format_hours(a.slots_kept + a.slots_lost)} -> "
                f"{format_hours(a.slots_kept)}, {'still' if left == a.total_chunks else 'now'} {sessions})")
    elif a.is_full_drop:
        what = f"Drop '{a.title}' entirely (-{format_hours(a.slots_lost)})"
    else:
        what = (f"Cut {a.chunks_cut} of {a.total_chunks} sessions of '{a.title}': "
                f"{format_hours(a.slots_kept + a.slots_lost)} -> {format_hours(a.slots_kept)} (-{format_hours(a.slots_lost)})")
    detail = f"priority {a.priority}, difficulty {a.difficulty}" + (", has a deadline" if a.has_deadline else "")
    return f"{what} [{detail}]"


def describe_proposal(n: int, p: DropProposal, new_task: DynamicTask) -> list[str]:
    """Lines for one numbered option: what changes, what happens to the new task, sleep, warnings."""
    tag = "  <- best" if n == 1 else ""
    lines = [f"{n}.{tag}"]
    about_new = f"priority {new_task.priority}, difficulty {new_task.difficulty}"
    if p.new_task_added:
        lines += [f"   - {_action_line(a)}" for a in p.actions]
        if p.new_task_slots_cut:
            lines.append(f"   - Adds '{new_task.title}' shortened to {format_hours(new_task.duration_slots - p.new_task_slots_cut)} "
                         f"(from {format_hours(new_task.duration_slots)}, still one block; {about_new})")
        else:
            lines.append(f"   - Adds '{new_task.title}' ({format_hours(new_task.duration_slots)}, {about_new})"
                         + (" and lets it use sleep below your target" if p.new_task_may_cut_sleep else ""))
    else:
        lines.append(f"   - Don't add '{new_task.title}' ({format_hours(new_task.duration_slots)}, "
                     f"{about_new}); nothing else changes")
    lines.append("   - Sleep: target kept" if p.sleep_sacrificed_slots == 0
                 else f"   - Sleep: {format_hours(p.sleep_sacrificed_slots)} below target")
    lines += [f"   !! {f}" for f in p.flags]
    return lines


def _ranked_options(report: DropReport, new_task: DynamicTask, must_add: bool) -> list[DropProposal]:
    """Best first. Shared by semi-automatic and automatic mode so both offer the same options."""
    options = [p for p in report.proposals if p.new_task_added or not must_add]
    if not must_add and not any(not p.new_task_added for p in options):
        options.append(dont_add_unverified(new_task, len(options) + 1))
    return options


def _offer(report: DropReport, new_task: DynamicTask, must_add: bool, show, best_only: bool) -> list[DropProposal]:
    """Show the ranked options (or only the best) and return them all; [] if there are none."""
    if report.fits_already:
        raise ValueError("everything already fits -- nothing to choose")
    options = _ranked_options(report, new_task, must_add)
    if not options:
        show(f"No combination of cuts found that fits '{new_task.title}'.")
        return []
    show(f"'{new_task.title}' does not fit as things stand. " + ("Best plan found:" if best_only else "Options, best first:"))
    for n, p in enumerate(options[:1] if best_only else options, 1):
        for line in describe_proposal(n, p, new_task):
            show(line)
    if not report.search_exhausted:
        show("Note: the search stopped at its limit, so "
             + ("a better plan may exist." if best_only else "better options may exist."))
    return options


def choose_drop_proposal(report: DropReport, new_task: DynamicTask, must_add: bool = False,
                         ask=input, show=print) -> DropProposal | None:
    """Semi-automatic: the student picks one of the ranked options."""
    options = _offer(report, new_task, must_add, show, best_only=False)
    if not options:
        return None
    while True:
        answer = ask(f"Pick 1-{len(options)} (Enter to cancel): ").strip()
        if answer == "":
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        show(f"Enter a number between 1 and {len(options)}.")


def choose_automatic(report: DropReport, new_task: DynamicTask, must_add: bool = False,
                     ask=input, show=print) -> DropProposal | None:
    """Automatic: shows the single best plan and applies it only on a "y"."""
    options = _offer(report, new_task, must_add, show, best_only=True)
    if not options or ask("Apply this plan? [y/N]: ").strip().lower() != "y":
        return None
    return options[0]


# ---------- manual: the student cuts, the solver only verifies ----------

class CutState:
    """durations: {task index: planned slots}. sizes_fn(index, remaining_slots) -> chunk sizes."""

    def __init__(self, durations: dict[int, int], sizes_fn):
        self.durations = dict(durations)
        self.sizes_fn = sizes_fn
        self.lost: dict[int, int] = {}          # total slots cut per task so far
        self.may_cut_sleep = False              # the new task may use sleep below target
        self._undo: list[tuple[dict[int, int], bool]] = []  # snapshots, one per successful edit

    def remaining(self, i: int) -> int:
        return self.durations[i] - self.lost.get(i, 0)

    def _snapshot(self) -> None:
        self._undo.append((dict(self.lost), self.may_cut_sleep))  # BEFORE changing, so undo is exact

    def _commit(self, i: int, slots: int) -> None:
        self._snapshot()
        self.lost[i] = self.lost.get(i, 0) + slots

    def _check(self, i: int) -> int:
        if i not in self.durations:
            raise ValueError("no such task")
        left = self.remaining(i)
        if left == 0:
            raise ValueError("that task is already fully cut")
        return left

    def drop(self, i: int) -> int:
        left = self._check(i)
        self._commit(i, left)
        return left

    def cut_chunks(self, i: int, k: int) -> int:
        self._check(i)
        sizes = self.sizes_fn(i, self.remaining(i))   # chunks of what is LEFT, not the original
        if not 1 <= k <= len(sizes):
            raise ValueError(f"cut between 1 and {len(sizes)} session(s)")
        lost = sum(sizes[-k:])
        self._commit(i, lost)
        return lost

    def reduce(self, i: int, slots: int) -> int:
        left = self._check(i)
        if not 1 <= slots < left:
            raise ValueError(f"reduce by 1 to {left - 1} slots (to remove it all, use drop)")
        self._commit(i, slots)
        return slots

    def allow_sleep(self) -> None:
        if self.may_cut_sleep:
            raise ValueError("it may already use sleep below target")
        self._snapshot()
        self.may_cut_sleep = True

    def undo(self) -> bool:
        if not self._undo:
            return False
        self.lost, self.may_cut_sleep = self._undo.pop()
        return True

    @property
    def touched(self) -> bool:
        return bool(self.lost) or self.may_cut_sleep


def _show_list(state: CutState, titles: dict, order: list, show) -> None:
    show("Your tasks (you can cut any of them, including the new one):")
    for n, i in enumerate(order, 1):
        left = state.remaining(i)
        if left == 0:
            what = "removed"
        else:
            k = len(state.sizes_fn(i, left))
            what = f"{format_hours(left)}, {k} session{'s' if k != 1 else ''}"
        was = f" (was {format_hours(state.durations[i])})" if state.lost.get(i) else ""
        sleep = ", may use sleep below target" if i == NEW_TASK and state.may_cut_sleep else ""
        show(f"  {n}. {titles[i]}{' (new)' if i == NEW_TASK else ''} -- {what}{was}{sleep}")


def _hours_or_error(text: str) -> int:
    try:
        return _hours(text)
    except ValueError:
        raise ValueError("enter hours as a number, like 1.5 (at least 0.25)") from None


def _edit_task(state: CutState, i: int, ask, show) -> bool:
    left = state.remaining(i)
    if left == 0:
        show("  That task is already fully cut. Undo to bring it back.")
        return False
    sessions = len(state.sizes_fn(i, left))
    may_allow_sleep = i == NEW_TASK and not state.may_cut_sleep
    options = (([] if i == NEW_TASK else ["[d]rop it"]) + (["[s]essions"] if sessions > 1 else []) + ["[t]ime"]
               + (["[z] let it use sleep below target"] if may_allow_sleep else []))
    raw = ask("  " + "  ".join(options) + "  (Enter to go back): ").strip().lower()
    try:
        if raw == "":
            return False
        if raw == "d" and i != NEW_TASK:
            state.drop(i)
        elif raw == "s" and sessions > 1:
            text = ask(f"  Cut how many of its {sessions} sessions (1-{sessions - 1})? ").strip()
            if not text.isdigit() or not 1 <= int(text) <= sessions - 1:
                raise ValueError(f"enter a whole number from 1 to {sessions - 1}")
            state.cut_chunks(i, int(text))
        elif raw == "t":
            state.reduce(i, _hours_or_error(ask(f"  Reduce by how many hours (up to {slots_to_hours(left - 1):g}, e.g. 1.5)? ").strip()))
        elif raw == "z" and may_allow_sleep:
            state.allow_sleep()
        else:
            show("  Choose one of the options shown.")
            return False
    except ValueError as e:
        show(f"  Not done: {e}")
        return False
    return True


def run_manual_edit(state: CutState, titles: dict, fits, ask=input, show=print,
                    must_add: bool = False) -> tuple[str, DropProposal | None]:
    """fits(lost, may_cut_sleep) -> a verified proposal if everything fits after those cuts (and
    with that leave for the new task), else None.
    Returns ("save", proposal), ("dont_add", None) or ("cancel", None). Nothing is saved here."""
    order = sorted(i for i in state.durations if i != NEW_TASK) + [NEW_TASK]
    result = None
    while True:
        _show_list(state, titles, order, show)
        if state.touched:
            if result:
                show("Everything fits now.")
                if result.sleep_sacrificed_slots:
                    show(f"  !! Sleep: {format_hours(result.sleep_sacrificed_slots)} below target")
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
                result = fits(state.lost, state.may_cut_sleep) if state.touched else None
                show("Undid the last edit.")
            else:
                show("Nothing to undo.")
        elif raw == "n" and not must_add:
            return "dont_add", None
        elif raw.isdigit() and 1 <= int(raw) <= len(order):
            if _edit_task(state, order[int(raw) - 1], ask, show):
                result = fits(state.lost, state.may_cut_sleep)
        else:
            show(f"Enter a number from 1 to {len(order)}" + (", or one of the letters shown." if extras else "."))
