import itertools  # NEW
from scheduler.models import (  # NEW
    DropAction, DropProposal, DropReport, DynamicTask, FixedBlock,
    ProfileSettings, SleepRule,
)
from scheduler.solver import build_schedule, sleep_warnings, split_sizes  # NEW

def chunk_sizes(task):  # NEW
    if task.splittable:
        return split_sizes(task.duration_slots, task.max_session_slots)
    return [task.duration_slots]

def cut_task(task, chunks_cut):  # NEW  -- method A
    sizes = chunk_sizes(task)
    if chunks_cut >= len(sizes):
        return None  # cutting every session = full drop
    return task.model_copy(update={"duration_slots": task.duration_slots - sum(sizes[-chunks_cut:])})

def loss_cost(task, lost_slots, settings):  # NEW
    per_slot = (task.priority ** 2 * settings.drop_priority_weight
                + task.difficulty * settings.drop_difficulty_weight)
    if task.deadline_day is not None:
        per_slot *= settings.drop_deadline_multiplier
    return per_slot * lost_slots

def _options(tasks, settings):  # NEW
    """Every way to cut each existing task: 1 session, 2 sessions, ... all of them."""
    out = {}
    for i, t in enumerate(tasks):
        sizes = chunk_sizes(t)
        out[i] = [
            DropAction(
                task_index=i, title=t.title, chunks_cut=k, total_chunks=len(sizes),
                slots_lost=sum(sizes[-k:]), slots_kept=t.duration_slots - sum(sizes[-k:]),
                priority=t.priority, difficulty=t.difficulty, has_deadline=t.deadline_day is not None,
            )
            for k in range(1, len(sizes) + 1)
        ]
    return out

def _apply(tasks, actions):  # NEW  -- returns the task list with the cuts applied
    cuts = {a.task_index: a.chunks_cut for a in actions}
    result = []
    for i, t in enumerate(tasks):
        smaller = cut_task(t, cuts[i]) if i in cuts else t
        if smaller is not None:  # None = fully dropped
            result.append(smaller)
    return result

def _dominated(cuts, feasible):  # NEW
    """True if an already-working plan cuts no more than this one, so this one is wasteful."""
    return any(all(cuts.get(i, 0) >= k for i, k in f.items()) for f in feasible)

def _solve_all_fit(fixed, tasks, num_days, sleep_rules, settings, limit):  # NEW
    """(items, sleep_warnings) if EVERY task gets placed, else None."""
    try:
        items, unscheduled = build_schedule(fixed, tasks, num_days=num_days, sleep_rules=sleep_rules,
                                            time_limit_seconds=limit, settings=settings)
    except RuntimeError:
        return None
    if unscheduled:
        return None
    return items, sleep_warnings(sleep_rules, items)

def _sleep_sacrificed(sleep_rules, items):  # NEW
    target = sum(r.length_slots for r in sleep_rules if not r.skip)
    slept = sum(i.end_slot - i.start_slot for i in items if i.kind == "sleep")
    return max(0, target - slept)

def propose_drops(fixed_blocks, tasks, new_task, num_days=1, sleep_rules=None,  # NEW
                  settings=None, must_add=False, max_actions=3, max_proposals=4,
                  max_checks=60, time_limit_seconds=5.0) -> DropReport:
    settings = settings or ProfileSettings()
    sleep_rules = sleep_rules or []
    base = build_schedule(fixed_blocks, tasks + [new_task], num_days=num_days,
                          sleep_rules=sleep_rules, time_limit_seconds=time_limit_seconds,
                          settings=settings)
    if not base[1]:  # everything already fits
        return DropReport(new_task_title=new_task.title, fits_already=True, proposals=[],
                          checks_used=1, search_exhausted=True)

    checks, found = 1, []

    def make(actions, new_added, items, warns):  # builds + scores one proposal
        sleep = _sleep_sacrificed(sleep_rules, items)
        flags = [w.message for w in warns if w.severity == "hard"]
        flags += [f"'{a.title}' would fall short of its deadline" for a in actions if a.has_deadline]
        if not new_added and new_task.deadline_day is not None:
            flags.append(f"'{new_task.title}' would not be done by its deadline")
        score = sum(loss_cost(tasks[a.task_index], a.slots_lost, settings) for a in actions)
        if not new_added:
            score += loss_cost(new_task, new_task.duration_slots, settings)
        score += settings.drop_sleep_weight * sleep + settings.drop_hard_flag_penalty * len(flags)
        return DropProposal(actions=list(actions), new_task_added=new_added, score=score,
                            slots_freed=sum(a.slots_lost for a in actions),
                            sleep_sacrificed_slots=sleep, flags=flags, schedule=items)

    # option: don't add the new task (valid only if the existing tasks fit without it)
    if not must_add:
        checks += 1
        r = _solve_all_fit(fixed_blocks, tasks, num_days, sleep_rules, settings, time_limit_seconds)
        if r:
            found.append(make((), False, *r))

    # options: cut existing tasks. Build every combo, cheapest first.
    opts = _options(tasks, settings)
    deficit = sum(t.duration_slots for t in base[1])
    sleep_flex = sum(r.length_slots - r.min_slots for r in sleep_rules if not r.skip)
    combos = []
    for n in range(1, max_actions + 1):
        for idxs in itertools.combinations(range(len(tasks)), n):
            for pick in itertools.product(*(opts[i] for i in idxs)):
                if sum(a.slots_lost for a in pick) < deficit - sleep_flex:
                    continue  # cannot possibly free enough room
                cost = sum(loss_cost(tasks[a.task_index], a.slots_lost, settings) for a in pick)
                combos.append((cost, pick))
    combos.sort(key=lambda c: c[0])

    feasible_cuts, exhausted = [], True
    for cost, pick in combos:
        cuts = {a.task_index: a.chunks_cut for a in pick}
        if _dominated(cuts, feasible_cuts):
            continue
        if checks >= max_checks:
            exhausted = False  # stopped by the budget; better options may exist
            break
        if len(feasible_cuts) >= max_proposals + 2:
            break  # enough candidates to rank
        checks += 1
        r = _solve_all_fit(fixed_blocks, _apply(tasks, pick) + [new_task], num_days,
                           sleep_rules, settings, time_limit_seconds)
        if r:  # only verified plans ever become proposals
            feasible_cuts.append(cuts)
            found.append(make(pick, True, *r))

    found.sort(key=lambda p: p.score)
    top = found[:max_proposals]
    for rank, p in enumerate(top, 1):
        p.rank = rank
    return DropReport(new_task_title=new_task.title, fits_already=False, proposals=top,
                      checks_used=checks, search_exhausted=exhausted)