from scheduler.models import DropAction, DropProposal, DropReport, DynamicTask
from scheduler.units import format_hours


def _action_line(a: DropAction) -> str:   
    if a.shrink:   
        what = (f"Shorten '{a.title}' by {format_hours(a.slots_lost)} ({format_hours(a.slots_kept + a.slots_lost)} -> "
                f"{format_hours(a.slots_kept)}, still one block)")
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
    if p.new_task_added:
        lines += [f"   - {_action_line(a)}" for a in p.actions]
        if p.new_task_slots_cut:
            lines.append(f"   - Adds '{new_task.title}' shortened to {format_hours(new_task.duration_slots - p.new_task_slots_cut)} "
                         f"(from {format_hours(new_task.duration_slots)}, still one block; priority {new_task.priority}, "
                         f"difficulty {new_task.difficulty})")
        else:
            lines.append(f"   - Adds '{new_task.title}' ({format_hours(new_task.duration_slots)}, "
                         f"priority {new_task.priority}, difficulty {new_task.difficulty})")
    else:
        lines.append(f"   - Don't add '{new_task.title}' ({format_hours(new_task.duration_slots)}, "
                     f"priority {new_task.priority}, difficulty {new_task.difficulty}); nothing else changes")
    lines.append("   - Sleep: target kept" if p.sleep_sacrificed_slots == 0
                 else f"   - Sleep: {format_hours(p.sleep_sacrificed_slots)} below target")
    lines += [f"   !! {f}" for f in p.flags]
    return lines


def _dont_add_fallback(new_task: DynamicTask, rank: int) -> DropProposal:   
    flags = ["Not verified: your existing tasks may still not all fit without it"]
    if new_task.deadline_day is not None:
        flags.append(f"'{new_task.title}' would not be done by its deadline")
    return DropProposal(rank=rank, actions=[], new_task_added=False, score=float("inf"),
                        slots_freed=0, sleep_sacrificed_slots=0, flags=flags, schedule=[])

def _ranked_options(report: DropReport, new_task: DynamicTask, must_add: bool) -> list[DropProposal]:   
    """Best first. Shared by semi-automatic and automatic mode so both offer the same options."""
    options = [p for p in report.proposals if p.new_task_added or not must_add]
    if not must_add and not any(not p.new_task_added for p in options):
        options.append(_dont_add_fallback(new_task, len(options) + 1))
    return options

def choose_drop_proposal(report: DropReport, new_task: DynamicTask, must_add: bool = False,
                         ask=input, show=print) -> DropProposal | None:   
    if report.fits_already:
        raise ValueError("everything already fits -- nothing to choose")
    options = _ranked_options(report, new_task, must_add)
    if not options:
        show(f"No combination of cuts found that fits '{new_task.title}'.")
        return None

    show(f"'{new_task.title}' does not fit as things stand. Options, best first:")
    for n, p in enumerate(options, 1):
        for line in describe_proposal(n, p, new_task):
            show(line)
    if not report.search_exhausted:
        show("Note: the search stopped at its limit, so better options may exist.")

    while True:
        answer = ask(f"Pick 1-{len(options)} (Enter to cancel): ").strip()
        if answer == "":
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        show(f"Enter a number between 1 and {len(options)}.")

def choose_automatic(report: DropReport, new_task: DynamicTask, must_add: bool = False,
                     ask=input, show=print) -> DropProposal | None:   
    """Same signature as choose_drop_proposal: shows the single best plan, applies it only on a "y"."""
    if report.fits_already:
        raise ValueError("everything already fits -- nothing to choose")
    options = _ranked_options(report, new_task, must_add)
    if not options:
        show(f"No combination of cuts found that fits '{new_task.title}'.")
        return None
    show(f"'{new_task.title}' does not fit as things stand. Best plan found:")
    for line in describe_proposal(1, options[0], new_task):
        show(line)
    if not report.search_exhausted:
        show("Note: the search stopped at its limit, so a better plan may exist.")
    if ask("Apply this plan? [y/N]: ").strip().lower() != "y":
        return None
    return options[0]