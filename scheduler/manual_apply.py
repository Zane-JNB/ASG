from scheduler.dropping import NEW_TASK as NEW, dont_add_unverified, try_cuts
from scheduler.manual_cuts import CutState
from scheduler.manual_review import run_manual_edit
from scheduler.models import DropAction, DropProposal
from scheduler.solver import chunk_sizes


def choose_manual(fit, must_add: bool = False, ask=input, show=print,
                  time_limit_seconds: float = 5.0) -> DropProposal | None:   
    tasks = fit.tasks  # index i == task_index in apply_drop_choice
    everyone = dict(enumerate(tasks)) | {NEW: fit.new_task}

    def sizes_fn(i, remaining):
        return chunk_sizes(everyone[i].model_copy(update={"duration_slots": remaining}))

    state = CutState({i: t.duration_slots for i, t in everyone.items()}, sizes_fn)
    titles = {i: t.title for i, t in everyone.items()}

    def fits(lost: dict[int, int]) -> DropProposal | None:
        actions = [DropAction(task_index=i, title=tasks[i].title, chunks_cut=0,
                              total_chunks=len(chunk_sizes(tasks[i])), slots_lost=n,
                              slots_kept=tasks[i].duration_slots - n, priority=tasks[i].priority,
                              difficulty=tasks[i].difficulty, has_deadline=tasks[i].deadline_day is not None,
                              shrink=n < tasks[i].duration_slots)
                   for i, n in sorted(lost.items()) if i != NEW]
        return try_cuts(fit.frame, tasks, fit.new_task, actions, lost.get(NEW, 0), time_limit_seconds)

    action, proposal = run_manual_edit(state, titles, fits, ask, show, must_add)
    if action == "dont_add":
        return dont_add_unverified(fit.new_task, 1)
    return proposal  # None for cancel