import functools
import itertools   
from scheduler.models import (  
    DropAction, DropProposal, DropReport, DynamicTask, FixedBlock,
    ProfileSettings, SleepRule,
)
from scheduler.solver import build_schedule, sleep_warnings, split_sizes   

def chunk_sizes(task):   
    if task.splittable:
        return split_sizes(task.duration_slots, task.max_session_slots)
    return [task.duration_slots]

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

def _sleep_sacrificed(sleep_rules, items):   
    target = sum(r.length_slots for r in sleep_rules if not r.skip)
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
                          checks_used=1, search_exhausted=True)
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

    # options: cut existing tasks. Build every combo, cheapest first.
       # options: cut existing tasks and/or shorten the new one, cheapest-looking first
    opts = _options(tasks, settings)
    new_cuts = [0] + (_shrink_amounts(new_task.duration_slots, settings)  #   -- a one-block new task can be shortened
                      if len(chunk_sizes(new_task)) == 1 else [])
    deficit = sum(t.duration_slots for t in base[1])
    sleep_flex = sum(r.length_slots - r.min_slots for r in sleep_rules if not r.skip)
    combos = []
    for n in range(0, max_actions + 1):  #   -- 0 = only shorten the new task
        for idxs in itertools.combinations(range(len(tasks)), n):
            for pick in itertools.product(*(opts[i] for i in idxs)):
                for new_cut in new_cuts:
                    if not pick and not new_cut:
                        continue  # that is the plan that already failed
                    if sum(a.slots_lost for a in pick) + new_cut < deficit - sleep_flex:
                        continue  # cannot possibly free enough room
                    cost = (sum(loss_cost(tasks[a.task_index], a.slots_lost, settings) for a in pick)
                            + loss_cost(new_task, new_cut, settings))
                    combos.append((cost, pick, new_cut))
    combos.sort(key=lambda c: c[0])

    feasible_cuts, exhausted = [], True
    for cost, pick, new_cut in combos:
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