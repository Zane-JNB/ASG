from scheduler.db import apply_plan_changes
from scheduler.models import DropProposal, DynamicTask, ExtractedTask


def apply_drop_choice(conn, student_id: int, proposal: DropProposal,
                      planned: list[tuple[int, DynamicTask]],
                      new_task: ExtractedTask | None = None) -> dict:  # NEW
    """`planned` is the (saved task id, DynamicTask) list given to propose_drops, same order.
    That is how a proposal's task_index maps back to a saved task."""
    if proposal.new_task_added and new_task is None:
        raise ValueError("this proposal adds the new task, but no new_task was given")
    cuts = {}
    for a in proposal.actions:
        task_id = planned[a.task_index][0]
        cuts[task_id] = cuts.get(task_id, 0) + a.slots_lost
    new_cut = proposal.new_task_slots_cut if proposal.new_task_added else 0  # NEW
    new_id = apply_plan_changes(conn, student_id, cuts, new_task if proposal.new_task_added else None, new_cut)
    if new_cut:
        cuts[new_id] = new_cut  # NEW -- reported like any other cut
    return {"cuts": cuts, "new_task_id": new_id}