"""Making room for a new task: ways to cut existing tasks (and shorten the new one), tried
cheapest first, or to let the new task use sleep below target; each verified by the solver and
scored for the student to choose from."""
import heapq
from scheduler.models import (
    DropAction, DropProposal, DropReport, DynamicTask, ProfileSettings, ScheduledItem, ScheduleWarning,
)
from scheduler.solver import FIT_CHECK_SECONDS, PlanFrame, chunk_sizes, merge_fixed_spans, reachable_sleep
from scheduler.units import SLOTS_PER_DAY

NEW_TASK = -1  # the task index that stands for the new task


def loss_cost(task: DynamicTask, lost_slots: int, settings: ProfileSettings) -> int:
    per_slot = (task.priority ** 2 * settings.drop_priority_weight
                + task.difficulty * settings.drop_difficulty_weight)
    if task.deadline_day is not None:
        per_slot *= settings.drop_deadline_multiplier
    return per_slot * lost_slots


def _shrink_amounts(duration: int, settings: ProfileSettings) -> list[int]:
    """How many slots a one-block task can lose and still exist (a full drop is a separate option)."""
    amounts = set()
    for f in settings.shrink_steps:
        lost = max(1, round(duration * f))
        if lost < duration:
            amounts.add(lost)
    return sorted(amounts)


def _options(tasks: list[DynamicTask], settings: ProfileSettings) -> dict[int, list[DropAction]]:
    """Every way to cut each existing task: whole sessions, or (for a one-block task) a shorter block."""
    out = {}
    for i, t in enumerate(tasks):
        sizes = chunk_sizes(t)
        base = dict(task_index=i, title=t.title, priority=t.priority, difficulty=t.difficulty,
                    has_deadline=t.deadline_day is not None)
        out[i] = [
            DropAction(chunks_cut=k, total_chunks=len(sizes), slots_lost=sum(sizes[-k:]),
                       slots_kept=t.duration_slots - sum(sizes[-k:]), **base)
            for k in range(1, len(sizes) + 1)
        ]
        if len(sizes) == 1:  # one block: it can also be shortened, staying one block
            out[i] += [DropAction(chunks_cut=0, total_chunks=1, slots_lost=x, slots_kept=t.duration_slots - x,
                                  shrink=True, **base)
                       for x in _shrink_amounts(t.duration_slots, settings)]
    return out


def _apply(tasks: list[DynamicTask], actions) -> list[DynamicTask]:
    lost = {a.task_index: a.slots_lost for a in actions}  # by slots, so cuts and shrinks work alike
    result = []
    for i, t in enumerate(tasks):
        remaining = t.duration_slots - lost.get(i, 0)
        if remaining > 0:
            result.append(t.model_copy(update={"duration_slots": remaining}))
    return result


def _dominated(cuts, feasible):
    """True if an already-working plan cuts no more than this one, so this one is wasteful."""
    return any(all(cuts.get(i, 0) >= k for i, k in f.items()) for f in feasible)


def _cut_combos(tasks: list[DynamicTask], new_task: DynamicTask, settings: ProfileSettings,
                max_actions: int, need: int):
    """Every way to cut up to max_actions existing tasks (one option each) and/or shorten the new
    task that frees at least `need` slots, as (cost, actions, new_cut), cheapest first. Equal costs
    keep a fixed order: fewest tasks cut, then by task index, option, and new-task cut.

    A lazy best-first walk, so only the combinations actually tried are ever built. The options
    are sorted by cost; a combination is a rising tuple of option positions plus a new-task cut,
    and each one comes from exactly one parent that costs no more: the same options with the
    previous new-task cut, or (with the new task uncut) one option fewer or its last option one
    step cheaper. Combinations that cut one task twice are walked through but never yielded."""
    options = sorted(((loss_cost(tasks[i], a.slots_lost, settings), i, k, a)
                      for i, acts in _options(tasks, settings).items() for k, a in enumerate(acts)),
                     key=lambda o: o[0])
    new_cuts = [0] + (_shrink_amounts(new_task.duration_slots, settings)  # a one-block new task can be shortened
                      if len(chunk_sizes(new_task)) == 1 else [])
    new_costs = [loss_cost(new_task, x, settings) for x in new_cuts]  # rising, like new_cuts

    def node(chosen: tuple[int, ...], cut: int):
        return sum(options[p][0] for p in chosen) + new_costs[cut], chosen, cut

    def children(chosen: tuple[int, ...], cut: int):
        if cut + 1 < len(new_cuts):
            yield chosen, cut + 1
        nxt = chosen[-1] + 1 if chosen else 0
        if cut == 0 and nxt < len(options):
            if len(chosen) < max_actions:
                yield chosen + (nxt,), 0
            if chosen:
                yield chosen[:-1] + (nxt,), 0

    heap, group, group_cost = [node((), 0)], [], 0
    while heap:
        cost, chosen, cut = heapq.heappop(heap)
        if cost != group_cost:  # every combination of the last cost has been found: release them in order
            yield from (combo for _, combo in sorted(group, key=lambda g: g[0]))
            group, group_cost = [], cost
        for child in children(chosen, cut):
            heapq.heappush(heap, node(*child))
        pick = sorted((options[p] for p in chosen), key=lambda o: o[1])
        if not (chosen or cut) or len({o[1] for o in pick}) < len(pick):
            continue  # the plan that already failed, or one task cut twice
        if sum(o[3].slots_lost for o in pick) + new_cuts[cut] < need:
            continue  # cannot possibly free enough room
        order = (len(pick), tuple(o[1] for o in pick), tuple(o[2] for o in pick), cut)
        group.append((order, (cost, tuple(o[3] for o in pick), new_cuts[cut])))
    yield from (combo for _, combo in sorted(group, key=lambda g: g[0]))


def _free_slots(frame: PlanFrame, tasks: list[DynamicTask]) -> int:
    """Slots in the window no fixed block covers, from the earliest any task may start.
    Ignores buffers, sleep and deadlines, so it over-counts the real room (a safe bound)."""
    lo = min((t.earliest_start for t in tasks), default=0)
    end = frame.num_days * SLOTS_PER_DAY
    covered = sum(max(0, min(e, end) - max(s, lo)) for s, e, _ in merge_fixed_spans(frame.fixed))
    return max(0, end - lo - covered)


def _sleep_sacrificed(sleep_rules, items: list[ScheduledItem]) -> int:
    """Sleep below what the nights allow; a cap from the morning after's early start isn't the cuts' fault."""
    target = sum(reachable_sleep(r) for r in sleep_rules)
    slept = sum(i.end_slot - i.start_slot for i in items if i.kind == "sleep")
    return max(0, target - slept)


def _proposal(frame: PlanFrame, tasks: list[DynamicTask], new_task: DynamicTask, actions, new_cut: int,
              solved: tuple[list[ScheduledItem], list[ScheduleWarning]], new_added: bool = True) -> DropProposal:
    """Score one verified plan: the time each task loses (by priority, difficulty and deadline),
    the sleep given up, and a penalty per hard problem the student must see."""
    items, warnings = solved
    s = frame.settings
    new_lost = new_cut if new_added else new_task.duration_slots
    flags = [w.message for w in warnings if w.severity == "hard"]
    flags += [f"'{a.title}' would fall short of its deadline" for a in actions if a.has_deadline]
    if new_lost and new_task.deadline_day is not None:
        flags.append(f"'{new_task.title}' would fall short of its deadline" if new_added
                     else f"'{new_task.title}' would not be done by its deadline")
    sleep = _sleep_sacrificed(frame.sleep_rules, items)
    score = (sum(loss_cost(tasks[a.task_index], a.slots_lost, s) for a in actions)
             + loss_cost(new_task, new_lost, s)
             + s.drop_sleep_weight * sleep + s.drop_hard_flag_penalty * len(flags))
    return DropProposal(actions=list(actions), new_task_added=new_added, new_task_slots_cut=new_cut,
                        new_task_may_cut_sleep=new_added and new_task.may_cut_sleep, score=score,
                        slots_freed=sum(a.slots_lost for a in actions), sleep_sacrificed_slots=sleep,
                        flags=flags, schedule=items)


def try_cuts(frame: PlanFrame, tasks: list[DynamicTask], new_task: DynamicTask, actions, new_cut: int = 0,
             time_limit_seconds: float = FIT_CHECK_SECONDS) -> DropProposal | None:
    """The scored proposal for these cuts (and new_cut slots off the new task) if everything then
    fits, else None. A new task with may_cut_sleep is tried with that leave."""
    shorter_new = new_task.model_copy(update={"duration_slots": new_task.duration_slots - new_cut})
    solved = frame.trial(_apply(tasks, actions) + [shorter_new], time_limit_seconds)
    return None if solved is None else _proposal(frame, tasks, new_task, actions, new_cut, solved)


def manual_actions(tasks: list[DynamicTask], lost: dict[int, int]) -> list[DropAction]:
    """The student's own cuts ({task index: slots cut}) as actions; the new task's cut is not one.
    A partial cut is a shrink by time; chunks_cut is how many sessions it removed."""
    actions = []
    for i, n in sorted(lost.items()):
        if i == NEW_TASK:
            continue
        t = tasks[i]
        kept = t.duration_slots - n
        before = len(chunk_sizes(t))
        after = len(chunk_sizes(t.model_copy(update={"duration_slots": kept}))) if kept else 0
        actions.append(DropAction(task_index=i, title=t.title, chunks_cut=before - after, total_chunks=before,
                                  slots_lost=n, slots_kept=kept, priority=t.priority, difficulty=t.difficulty,
                                  has_deadline=t.deadline_day is not None, shrink=kept > 0))
    return actions


def dont_add_unverified(new_task: DynamicTask, rank: int) -> DropProposal:
    """"Don't add the new task" when the search never checked that the others fit without it."""
    flags = ["Not verified: your existing tasks may still not all fit without it"]
    if new_task.deadline_day is not None:
        flags.append(f"'{new_task.title}' would not be done by its deadline")
    return DropProposal(rank=rank, actions=[], new_task_added=False, score=float("inf"),
                        slots_freed=0, sleep_sacrificed_slots=0, flags=flags, schedule=[])


def propose_drops(frame: PlanFrame, tasks: list[DynamicTask], new_task: DynamicTask, must_add: bool = False,
                  max_actions: int = 3, max_proposals: int = 4, max_checks: int = 60,
                  time_limit_seconds: float = FIT_CHECK_SECONDS) -> DropReport:
    """The best ways to make room for new_task, best first. RuntimeError if the first check (as
    things stand) finds no plan in time: then the fit can't be checked at all."""
    if frame.solve_all_fit(tasks + [new_task], time_limit_seconds):
        return DropReport(new_task_title=new_task.title, fits_already=True, proposals=[],
                          checks_used=1, search_exhausted=True)

    checks, found, exhausted = 1, [], True
    if not must_add:
        checks += 1
        solved = frame.trial(tasks, time_limit_seconds)
        if solved:
            found.append(_proposal(frame, tasks, new_task, (), 0, solved, new_added=False))
    if checks < max_checks:  # nothing cut: the new task may use sleep below target instead
        checks += 1
        sleepy = try_cuts(frame, tasks, new_task.model_copy(update={"may_cut_sleep": True}), (), 0,
                          time_limit_seconds)
        if sleepy:
            found.append(sleepy)
    else:
        exhausted = False

    # the least that must be freed: all task time minus every free slot (an over-count of the room)
    need = sum(t.duration_slots for t in tasks + [new_task]) - _free_slots(frame, tasks + [new_task])
    feasible_cuts = []
    for _, pick, new_cut in _cut_combos(tasks, new_task, frame.settings, max_actions, need):
        cuts = {a.task_index: a.slots_lost for a in pick}
        if new_cut:
            cuts[NEW_TASK] = new_cut
        if _dominated(cuts, feasible_cuts):
            continue
        if checks >= max_checks:
            exhausted = False  # stopped by the budget, so better options may exist
            break
        if len(feasible_cuts) >= max_proposals + 2:
            break  # enough candidates to rank
        checks += 1
        proposal = try_cuts(frame, tasks, new_task, pick, new_cut, time_limit_seconds)
        if proposal:
            feasible_cuts.append(cuts)
            found.append(proposal)

    found.sort(key=lambda p: p.score)
    top = found[:max_proposals]
    for rank, p in enumerate(top, 1):
        p.rank = rank
    return DropReport(new_task_title=new_task.title, fits_already=False, proposals=top,
                      checks_used=checks, search_exhausted=exhausted)
