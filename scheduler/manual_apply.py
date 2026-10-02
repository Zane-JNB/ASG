from scheduler.drop_review import _dont_add_fallback  # NEW
from scheduler.dropping import _apply, _solve_all_fit, chunk_sizes, make_proposal  # NEW
from scheduler.manual_cuts import NEW, CutState  # NEW
from scheduler.manual_review import run_manual_edit  # NEW
from scheduler.models import DropAction, DropProposal  # NEW


def choose_manual(fit, must_add: bool = False, ask=input, show=print,
                  time_limit_seconds: float = 5.0) -> DropProposal | None:  # NEW
    tasks = [t for _, t in fit.planned]  # index i == task_index in apply_drop_choice
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
        new_cut = lost.get(NEW, 0)
        shorter_new = fit.new_task.model_copy(update={"duration_slots": fit.new_task.duration_slots - new_cut})
        r = _solve_all_fit(fit.fixed, _apply(tasks, tuple(actions)) + [shorter_new], fit.anchor.num_days,
                           fit.sleep_rules, fit.settings, time_limit_seconds)
        if not r:
            return None
        return make_proposal(tasks, fit.new_task, fit.settings, fit.sleep_rules, tuple(actions), True, new_cut, *r)

    action, proposal = run_manual_edit(state, titles, fits, ask, show, must_add)
    if action == "dont_add":
        return _dont_add_fallback(fit.new_task, 1)
    return proposal  # None for cancel