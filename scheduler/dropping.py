import functools
import heapq
import itertools
from scheduler.models import (  
    DropAction, DropProposal, DropReport, DynamicTask, FixedBlock,
    ProfileSettings, SleepRule,
)
from scheduler.units import SLOTS_PER_DAY
from scheduler.solver import build_schedule, chunk_sizes, merge_fixed_spans, reachable_sleep, sleep_warnings

def cut_task(task, chunks_cut):   
    sizes = chunk_sizes(task)
    if chunks_cut >= len(sizes):
        return None  # cutting every session = full drop
    return task.model_copy(update={"duration_slots": task.duration_slots - sum(sizes[-chunks_cut:])})

def loss_cost(task, lost_slots, settings):  
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
        if len(sizes) == 1:  #   one block: it can also be shortened, staying one block
            out[i] += [DropAction(chunks_cut=0, total_chunks=1, slots_lost=x, slots_kept=t.duration_slots - x,
                                  shrink=True, **base)
                       for x in _shrink_amounts(t.duration_slots, settings)]
    return out


def _apply(tasks: list[DynamicTask], actions: tuple[DropAction, ...]) -> list[DynamicTask]:
    lost = {a.task_index: a.slots_lost for a in actions}  #   -- by slots, so cuts and shrinks work alike
    result = []
    for i, t in enumerate(tasks):
        remaining = t.duration_slots - lost.get(i, 0)
        if remaining > 0:
            result.append(t.model_copy(update={"duration_slots": remaining}))
    return result

def _dominated(cuts, feasible):   
    """True if an already-working plan cuts no more than this one, so this one is wasteful."""
    return any(all(cuts.get(i, 0) >= k for i, k in f.items()) for f in feasible)

def _cheapest_first(make_combos, k):
    """Yield make_combos() cheapest first (ties keep their order), like a full sort, but only ever
    holding the k cheapest in memory. If all k get used, the next batch is found by doubling k."""
    done = 0
    while True:
        batch = heapq.nsmallest(k, make_combos(), key=lambda c: c[0])  # same as sorted(...)[:k]
        yield from batch[done:]
        if len(batch) < k:
            return
        done, k = k, 2 * k

def _solve_all_fit(fixed, tasks, num_days, sleep_rules, settings, limit):   
    """(items, sleep_warnings) if EVERY task gets placed, else None."""
    try:
        items, unscheduled = build_schedule(fixed, tasks, num_days=num_days, sleep_rules=sleep_rules,
                                            time_limit_seconds=limit, settings=settings)
    except RuntimeError:
        return None
    if unscheduled:
        return None
    return items, sleep_warnings(sleep_rules, items)

def _free_slots(fixed_blocks, tasks, num_days):
    """Slots in the window no fixed block covers, from the earliest any task may start.
    Ignores buffers, sleep and deadlines, so it over-counts the real room (a safe bound)."""
    lo = min((0 if t.earliest_start_day is None else t.earliest_start_day * SLOTS_PER_DAY + t.earliest_start_slot
              for t in tasks), default=0)
    end = num_days * SLOTS_PER_DAY
    covered = sum(max(0, min(e, end) - max(s, lo)) for s, e, _ in merge_fixed_spans(fixed_blocks))
    return max(0, end - lo - covered)

def _sleep_sacrificed(sleep_rules, items):   
    """Sleep below what the nights allow; a cap from the morning after's early start isn't the cuts' fault."""
    target = sum(reachable_sleep(r) for r in sleep_rules)
    slept = sum(i.end_slot - i.start_slot for i in items if i.kind == "sleep")
    return max(0, target - slept)

def make_proposal(tasks, new_task, settings, sleep_rules, actions, new_added, new_cut, items, warns): # builds + scores one proposal
        sleep = _sleep_sacrificed(sleep_rules, items)
        flags = [w.message for w in warns if w.severity == "hard"]
        flags += [f"'{a.title}' would fall short of its deadline" for a in actions if a.has_deadline]
        if not new_added and new_task.deadline_day is not None:
            flags.append(f"'{new_task.title}' would not be done by its deadline")
        score = sum(loss_cost(tasks[a.task_index], a.slots_lost, settings) for a in actions)
        if not new_added:
            score += loss_cost(new_task, new_task.duration_slots, settings)
        if new_cut and new_task.deadline_day is not None: 
            flags.append(f"'{new_task.title}' would fall short of its deadline")
        score += loss_cost(new_task, new_cut, settings)
        score += settings.drop_sleep_weight * sleep + settings.drop_hard_flag_penalty * len(flags)
        freed = sum(a.slots_lost for a in actions)
        return DropProposal(actions=list(actions), new_task_added=new_added, new_task_slots_cut=new_cut,
                            score=score,slots_freed=freed, sleep_sacrificed_slots=sleep, flags=flags,
                            schedule=items)

def already_unplaced(fixed_blocks, tasks, num_days=1, sleep_rules=None, settings=None,
                     time_limit_seconds=5.0) -> list[int]:
    """Indices of the tasks that don't fit even without a new one (e.g. due too soon)."""
    _, unscheduled = build_schedule(fixed_blocks, tasks, num_days=num_days, sleep_rules=sleep_rules or [],
                                    time_limit_seconds=time_limit_seconds, settings=settings)
    out = {id(t) for t in unscheduled}
    return [i for i, t in enumerate(tasks) if id(t) in out]

def propose_drops(fixed_blocks, tasks, new_task, num_days=1, sleep_rules=None,   
                  settings=None, must_add=False, max_actions=3, max_proposals=4,
                  max_checks=60, time_limit_seconds=5.0, search = True) -> DropReport:
    settings = settings or ProfileSettings()
    sleep_rules = sleep_rules or []
    base = build_schedule(fixed_blocks, tasks + [new_task], num_days=num_days,
                          sleep_rules=sleep_rules, time_limit_seconds=time_limit_seconds,
                          settings=settings)
    if not base[1]: 
        return DropReport(new_task_title=new_task.title, fits_already=True, proposals=[],
                          checks_used=1, search_exhausted=True,
                          fit_warnings=sleep_warnings(sleep_rules, base[0]))
    if not search:  
        return DropReport(new_task_title=new_task.title, fits_already=False, proposals=[],
                          checks_used=1, search_exhausted=False)

    checks, found = 1, []
    make = functools.partial(make_proposal, tasks, new_task, settings, sleep_rules)

    if not must_add:
        checks += 1
        r = _solve_all_fit(fixed_blocks, tasks, num_days, sleep_rules, settings, time_limit_seconds)
        if r:
            found.append(make((), False, 0, *r))    

    # options: cut existing tasks and/or shorten the new one, cheapest-looking first
    opts = _options(tasks, settings)
    new_cuts = [0] + (_shrink_amounts(new_task.duration_slots, settings)  #   -- a one-block new task can be shortened
                      if len(chunk_sizes(new_task)) == 1 else [])
    # the least that must be freed: all task time minus every free slot (an over-count of the room)
    need = (sum(t.duration_slots for t in tasks + [new_task])
            - _free_slots(fixed_blocks, tasks + [new_task], num_days))

    def combos():
        for n in range(0, max_actions + 1):  #   -- 0 = only shorten the new task
            for idxs in itertools.combinations(range(len(tasks)), n):
                for pick in itertools.product(*(opts[i] for i in idxs)):
                    for new_cut in new_cuts:
                        if not pick and not new_cut:
                            continue  # that is the plan that already failed
                        if sum(a.slots_lost for a in pick) + new_cut < need:
                            continue  # cannot possibly free enough room
                        cost = (sum(loss_cost(tasks[a.task_index], a.slots_lost, settings) for a in pick)
                                + loss_cost(new_task, new_cut, settings))
                        yield cost, pick, new_cut

    feasible_cuts, exhausted = [], True
    for cost, pick, new_cut in _cheapest_first(combos, 4 * max_checks):
        cuts = {a.task_index: a.slots_lost for a in pick}
        if new_cut:
            cuts[-1] = new_cut  # -1 stands for the new task
        if _dominated(cuts, feasible_cuts):
            continue
        if checks >= max_checks:
            exhausted = False  # stopped by the budget, so better options may exist
            break
        if len(feasible_cuts) >= max_proposals + 2:
            break  # enough candidates to rank
        checks += 1
        shorter_new = new_task.model_copy(update={"duration_slots": new_task.duration_slots - new_cut})
        r = _solve_all_fit(fixed_blocks, _apply(tasks, pick) + [shorter_new], num_days,
                           sleep_rules, settings, time_limit_seconds)
        if r:
            feasible_cuts.append(cuts)
            found.append(make(pick, True, new_cut, *r))

    found.sort(key=lambda p: p.score)
    top = found[:max_proposals]
    for rank, p in enumerate(top, 1):
        p.rank = rank
    return DropReport(new_task_title=new_task.title, fits_already=False, proposals=top,
                      checks_used=checks, search_exhausted=exhausted)