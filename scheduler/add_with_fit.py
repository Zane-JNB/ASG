"""Adding a task: check that it fits; if not, the student makes room (manual, semi-automatic or
automatic) and the choice is saved as plan cuts, all in one transaction."""
from datetime import datetime
from scheduler.db import EXTRACTED_TASKS, apply_plan_changes
from scheduler.dropping import NEW_TASK, dont_add_unverified, manual_actions, propose_drops, try_cuts
from scheduler.fit_check import FitInputs, build_fit_inputs
from scheduler.make_room import CutState, ask_mode, choose_automatic, choose_drop_proposal, run_manual_edit
from scheduler.models import DropProposal, DynamicTask, ExtractedTask
from scheduler.solver import chunk_sizes
from scheduler.units import format_hours


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

    full = None  # the ranked search, run at most once and shared by semi and automatic
    while True:  # declining or cancelling a mode returns to the mode prompt
        mode = ask_mode(ask, show)
        if mode is None:
            show("Cancelled -- nothing saved.")
            return None
        if mode == "m":
            choice = _choose_manual(fit, must_add, ask, show)
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


def _choose_manual(fit: FitInputs, must_add: bool, ask, show) -> DropProposal | None:
    """Manual: the student cuts any task (the new one too); the solver only verifies."""
    tasks = fit.tasks  # index i == task_index in apply_drop_choice
    everyone: dict[int, DynamicTask] = dict(enumerate(tasks)) | {NEW_TASK: fit.new_task}

    def sizes_fn(i, remaining):
        return chunk_sizes(everyone[i].model_copy(update={"duration_slots": remaining}))

    def fits(lost: dict[int, int]) -> DropProposal | None:
        return try_cuts(fit.frame, tasks, fit.new_task, manual_actions(tasks, lost), lost.get(NEW_TASK, 0))

    state = CutState({i: t.duration_slots for i, t in everyone.items()}, sizes_fn)
    action, proposal = run_manual_edit(state, {i: t.title for i, t in everyone.items()}, fits, ask, show, must_add)
    if action == "dont_add":
        return dont_add_unverified(fit.new_task, 1)
    return proposal  # None for cancel


def apply_drop_choice(conn, student_id: int, proposal: DropProposal,
                      planned: list[tuple[int, DynamicTask]],
                      new_task: ExtractedTask | None = None) -> dict:
    """`planned` is the (saved task id, DynamicTask) list given to propose_drops, same order.
    That is how a proposal's task_index maps back to a saved task."""
    if proposal.new_task_added and new_task is None:
        raise ValueError("this proposal adds the new task, but no new_task was given")
    cuts = {}
    for a in proposal.actions:
        task_id = planned[a.task_index][0]
        cuts[task_id] = cuts.get(task_id, 0) + a.slots_lost
    new_cut = proposal.new_task_slots_cut if proposal.new_task_added else 0
    new_id = apply_plan_changes(conn, student_id, cuts, new_task if proposal.new_task_added else None, new_cut)
    if new_cut:
        cuts[new_id] = new_cut
    return {"cuts": cuts, "new_task_id": new_id}
