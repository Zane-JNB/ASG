from datetime import datetime

from scheduler.db import add_extracted_task
from scheduler.drop_apply import apply_drop_choice
from scheduler.drop_review import _h, choose_drop_proposal, choose_automatic
from scheduler.dropping import propose_drops
from scheduler.fit_check import build_fit_inputs
from scheduler.models import ExtractedTask
from scheduler.manual_apply import choose_manual 
from scheduler.manual_review import ask_mode 


def add_task_with_fit(conn, student_id: int, new_task: ExtractedTask, now: datetime,
                      ask=input, show=print, must_add: bool = False) -> dict | None:  # NEW
    """Only asks the student anything if the deadline can't be met.
    Returns {"cuts": {...}, "new_task_id": id or None}, or None if nothing was saved."""
    try:
        fit = build_fit_inputs(conn, student_id, now, new_task)
        report = propose_drops(fit.fixed, [t for _, t in fit.planned], fit.new_task, fit.anchor.num_days,
                               fit.sleep_rules, settings=fit.settings, must_add=must_add, search = False)
    except (ValueError, RuntimeError) as e:
        show(f"Could not check the fit: {e}")
        show("Nothing saved.")
        return None

    if report.fits_already:  # later-deadline tasks were shuffled by the solver if needed
        new_id = add_extracted_task(conn, student_id, new_task)
        show("Added.")
        return {"cuts": {}, "new_task_id": new_id}

    full = None  # NEW -- the ranked search, run at most once and shared by semi and automatic
    while True:  # NEW -- declining or cancelling a mode returns to the mode prompt
        mode = ask_mode(ask, show)
        if mode is None:
            show("Cancelled -- nothing saved.")
            return None
        if mode == "m":  # manual: the student cuts, the solver only verifies
            choice = choose_manual(fit, must_add, ask, show)
        else:  # semi and automatic share one search; they differ in who picks
            if full is None:  # NEW
                full = propose_drops(fit.fixed, [t for _, t in fit.planned], fit.new_task, fit.anchor.num_days,
                                     fit.sleep_rules, settings=fit.settings, must_add=must_add)
            pick = choose_automatic if mode == "a" else choose_drop_proposal
            choice = pick(full, fit.new_task, must_add, ask, show)
        if choice is not None:
            break
        show("Nothing saved yet. Choose another way, or Enter to cancel.")  # NEW
    summary = apply_drop_choice(conn, student_id, choice, fit.planned, new_task)  # unchanged from here down
    if summary["new_task_id"] is None:
        show(f"'{new_task.title}' was not added. Nothing else changed.")
    else:
        show(f"Added '{new_task.title}'.")
        for a in choice.actions:
            show(f"  For this plan only: '{a.title}' -{_h(a.slots_lost)} (its saved hours are unchanged)")
        if choice.new_task_slots_cut:  # NEW
            full = new_task.duration_slots
            show(f"  For this plan only: '{new_task.title}' is planned at {_h(full - choice.new_task_slots_cut)} "
                 f"(saved as {_h(full)})")
    return summary