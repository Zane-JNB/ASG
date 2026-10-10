from datetime import datetime
from scheduler.db import EXTRACTED_TASKS
from scheduler.drop_apply import apply_drop_choice
from scheduler.drop_review import choose_drop_proposal, choose_automatic
from scheduler.units import format_hours
from scheduler.dropping import propose_drops
from scheduler.fit_check import build_fit_inputs
from scheduler.models import ExtractedTask
from scheduler.manual_apply import choose_manual 
from scheduler.manual_review import ask_mode 


def add_task_with_fit(conn, student_id: int, new_task: ExtractedTask, now: datetime,
                      ask=input, show=print, must_add: bool = False) -> dict | None:   
    """Only asks the student anything if the deadline can't be met.
    Returns {"cuts": {...}, "new_task_id": id or None}, or None if nothing was saved."""
    try:
        fit = build_fit_inputs(conn, student_id, now, new_task)
        fits = fit.frame.solve_all_fit(fit.tasks + [fit.new_task])
        if fits is None:  # only the new task may decide: leave out tasks that don't fit anyway
            stuck = fit.frame.unplaced(fit.tasks)
            if stuck:
                for i in stuck:
                    show(f"Warning: '{fit.tasks[i].title}' can't fit in the plan even without "
                         f"'{new_task.title}'; it's left out of this check.")
                fit = fit.without(stuck)
                fits = fit.frame.solve_all_fit(fit.tasks + [fit.new_task])
    except (ValueError, RuntimeError) as e:  # the fit could not be checked
        show(f"Could not check the fit: {e}")
        show("Nothing saved.")
        return None
    for w in fit.warnings:  # the fit was checked without these rows
        if w.kind == "saved_row_unreadable":
            show(f"Warning: {w.message}")

    if fits is not None:  # later-deadline tasks were shuffled by the solver if needed
        new_id = EXTRACTED_TASKS.add(conn, student_id, new_task)
        show("Added.")
        for w in fits[1]:  # it fits, but sleep was given up for it
            if w.kind == "sleep_short" or w.severity == "hard":
                show(f"Warning: {w.message}")
        return {"cuts": {}, "new_task_id": new_id}

    full = None  #  the ranked search, run at most once and shared by semi and automatic
    while True:  #  declining or cancelling a mode returns to the mode prompt
        mode = ask_mode(ask, show)
        if mode is None:
            show("Cancelled -- nothing saved.")
            return None
        if mode == "m":  # manual: the student cuts, the solver only verifies
            choice = choose_manual(fit, must_add, ask, show)
        else:  # semi and automatic share one search; they differ in who picks
            if full is None:   
                try:
                    full = propose_drops(fit.frame, fit.tasks, fit.new_task, must_add=must_add)
                except (ValueError, RuntimeError) as e:  # e.g. the solver ran out of time
                    show(f"Could not search for ways to make room: {e}")
            if full is None:
                choice = None
            else:
                pick = choose_automatic if mode == "a" else choose_drop_proposal
                choice = pick(full, fit.new_task, must_add, ask, show)
        if choice is not None:
            break
        show("Nothing saved yet. Choose another way, or Enter to cancel.")   
    summary = apply_drop_choice(conn, student_id, choice, fit.planned, new_task)   
    if summary["new_task_id"] is None:
        show(f"'{new_task.title}' was not added. Nothing else changed.")
    else:
        show(f"Added '{new_task.title}'.")
        for a in choice.actions:
            show(f"  For this plan only: '{a.title}' -{format_hours(a.slots_lost)} (its saved hours are unchanged)")
        if choice.new_task_slots_cut:   
            full_slots = new_task.duration_slots
            show(f"  For this plan only: '{new_task.title}' is planned at {format_hours(full_slots - choice.new_task_slots_cut)} "
                 f"(saved as {format_hours(full_slots)})")
    return summary