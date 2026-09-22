import math
from ortools.sat.python import cp_model
from scheduler.models import (
    DynamicTask, FixedBlock, ScheduledItem, SleepRule, ScheduleWarning,
    SLOTS_PER_DAY, MINUTES_PER_SLOT, slot_to_time,
)

presence_bonus = 10_000  # big, so fitting a task always beats moving things earlier
sleep_min_penalty = 1_000_000  #   per slot below minimum sleep: outweighs any pile of tasks
sleep_target_penalty = 5_000  #   per slot between minimum and target sleep
bedtime_penalty = 50  #   per slot away from the preferred bedtime

def split_sizes(duration: int, max_session: int) -> list[int]:
    """Fewest, most even chunks that are each <= max_session."""
    n = math.ceil(duration / max_session)
    base, extra = divmod(duration, n)
    return [base + 1] * extra + [base] * (n - extra)

def hours(slots: int) -> str:  
    return f"{slots * MINUTES_PER_SLOT / 60:.2g}h"  

def plan_day_cp(fixed_blocks: list[FixedBlock], tasks: list[DynamicTask],
                buffer_slots: int = 1, num_days: int = 1,
                sleep_rules: list[SleepRule] | None = None): 
    
    model = cp_model.CpModel()
    intervals = []
 
    # fixed blocks: intervals that can't move
    for block in fixed_blocks:
        if block.day >= num_days:
            raise ValueError(
                f"'{block.title}' is on day {block.day}, but the plan has {num_days} day(s)"
            )
        length = block.end_slot - block.start_slot
        start = block.day * SLOTS_PER_DAY + block.start_slot
        intervals.append(model.NewFixedSizeIntervalVar(start, length, block.title))
 
    # tasks: each becomes one or more chunks the solver places
    placed = []  # (task, is_present, [(start_expr, size), ...])
    for i, task in enumerate(tasks):
        is_present = model.NewBoolVar(f"present_{i}")
 
        if task.splittable:
            sizes = split_sizes(task.duration_slots, task.max_session_slots)
        else:
            sizes = [task.duration_slots]
 
        chunks = []
        for j, size in enumerate(sizes):
            start = model.NewIntVar(0, num_days * SLOTS_PER_DAY - size, f"start_{i}_{j}")
 
            intervals.append(
                model.NewOptionalFixedSizeIntervalVar(
                    start, size + buffer_slots, is_present, f"task_{i}_{j}"
                )
            )
 
            # rule: finish before the deadline
            if task.deadline_day is not None:
                deadline_end = task.deadline_day * SLOTS_PER_DAY + task.deadline_slot
                model.Add(start + size <= deadline_end).OnlyEnforceIf(is_present)
 
            # rule: parts of one task stay in order
            if chunks:
                prev_start, prev_size = chunks[-1]
                model.Add(
                    start >= prev_start + prev_size + buffer_slots
                ).OnlyEnforceIf(is_present)
 
            chunks.append((start, size))
 
        placed.append((task, is_present, chunks))
 
    # sleep: one flexible interval per night (the solver picks bedtime and length) 
    sleep_placed = []  # (rule, start, size) 
    sleep_cost = []  #   terms subtracted from the objective
    seen_nights = set()  
    for rule in sleep_rules or []:  
        if rule.night >= num_days: 
            raise ValueError(  
                f"sleep rule is for night {rule.night}, but the plan has {num_days} day(s)"  
            )   
        if rule.night in seen_nights:  
            raise ValueError(f"two sleep rules for night {rule.night}")   
        seen_nights.add(rule.night)   
        if rule.skip:  #    no sleep interval at all: the night is free for tasks
            continue   
 
        base = rule.night * SLOTS_PER_DAY   
        start = model.NewIntVar(   
            base + rule.earliest_bed, base + rule.latest_bed, f"sleep_start_{rule.night}"   
        )   
        size = model.NewIntVar(0, rule.length_slots, f"sleep_size_{rule.night}")   
        end = model.NewIntVar(  
            base + rule.earliest_bed, base + rule.latest_bed + rule.length_slots,  
            f"sleep_end_{rule.night}",  
        )  
        intervals.append(model.NewIntervalVar(start, size, end, f"sleep_{rule.night}"))   
 
        # how far below the minimum (0 if the minimum is met)
        shortfall = model.NewIntVar(0, rule.min_slots, f"sleep_short_{rule.night}")   
        model.Add(shortfall >= rule.min_slots - size)  
        # how far the bedtime is from the preferred one
        drift = model.NewIntVar(0, 2 * SLOTS_PER_DAY, f"bed_drift_{rule.night}")   
        model.AddAbsEquality(drift, start - (base + rule.preferred_bed))   
 
        sleep_cost.append(sleep_min_penalty * shortfall)  
        sleep_cost.append(sleep_target_penalty * (rule.length_slots - size))   
        sleep_cost.append(bedtime_penalty * drift)   
        sleep_placed.append((rule, start, size))   
 
    # rule: nothing overlaps
    model.AddNoOverlap(intervals)
 
    # goal: fit as many high-priority tasks as possible, and place them early
    model.Maximize(
        sum(
            task.priority * presence_bonus * present
            - task.priority * sum(start for start, _ in chunks)
            for task, present, chunks in placed
        )
        - sum(sleep_cost)   
    )
 
    solver = cp_model.CpSolver()
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError("No valid schedule (do your fixed blocks overlap?)")
 
    items = [
        ScheduledItem(
            title=b.title,
            start_slot=b.start_slot,
            end_slot=b.end_slot,
            kind="fixed",
            day=b.day,
        )
        for b in fixed_blocks
    ]
 
    for rule, start, size in sleep_placed:   
        length = solver.Value(size)  
        if length == 0:  #    no room at all; sleep_warnings will report it
            continue   
        day, offset = divmod(solver.Value(start), SLOTS_PER_DAY)   
        items.append(   
            ScheduledItem(   
                title="Sleep", start_slot=offset, end_slot=offset + length,   
                kind="sleep", day=day,   
            )   
        )   
 
    unscheduled = []
    for task, present, chunks in placed:
        if not solver.Value(present):
            unscheduled.append(task)
            continue
        for j, (start, size) in enumerate(chunks):
            day, offset = divmod(solver.Value(start), SLOTS_PER_DAY)
            title = (
                task.title
                if len(chunks) == 1
                else f"{task.title} ({j + 1}/{len(chunks)})"
            )
            items.append(
                ScheduledItem(
                    title=title,
                    start_slot=offset,
                    end_slot=offset + size,
                    kind="task",
                    day=day,
                )
            )
 
    items.sort(key=lambda i: (i.day, i.start_slot))
    return items, unscheduled

def sleep_warnings(sleep_rules: list[SleepRule],   
                   items: list[ScheduledItem]) -> list[ScheduleWarning]:   
    """Check the finished plan against each night's rule and report anything given up."""   
    warnings = []   
    for rule in sorted(sleep_rules, key=lambda r: r.night):   
        label = f"Night {rule.night}"   
        if rule.skip:   
            warnings.append(ScheduleWarning(   
                severity="hard", kind="sleep_skipped",   
                message=f"{label}: sleep skipped. Tasks may run through the night.",   
            ))   
            continue   
 
        # find this night's sleep item: its start falls inside this night's bedtime window
        base = rule.night * SLOTS_PER_DAY   
        found = None   
        for item in items:   
            abs_start = item.day * SLOTS_PER_DAY + item.start_slot   
            if (item.kind == "sleep"   
                    and base + rule.earliest_bed <= abs_start <= base + rule.latest_bed):   
                found = item   
                break   
 
        length = 0 if found is None else found.end_slot - found.start_slot   
        if length < rule.min_slots:   
            warnings.append(ScheduleWarning(   
                severity="hard", kind="sleep_short",   
                message=(f"{label}: only {hours(length)} of sleep fits, below your minimum "   
                         f"of {hours(rule.min_slots)}."),   
            ))   
        elif length < rule.length_slots:   
            warnings.append(ScheduleWarning(   
                severity="soft", kind="sleep_short",   
                message=(f"{label}: {hours(length)} of sleep, shorter than your "   
                         f"target of {hours(rule.length_slots)}."),   
            ))   
 
        if found is not None:  
            bed = found.day * SLOTS_PER_DAY + found.start_slot - base   
            if bed > rule.preferred_bed:   
                warnings.append(ScheduleWarning(   
                    severity="soft", kind="late_bedtime",  
                    message=(f"{label}: bedtime is {slot_to_time(bed % SLOTS_PER_DAY)}, "   
                             f"later than your preferred "  
                             f"{slot_to_time(rule.preferred_bed % SLOTS_PER_DAY)}."),  
                ))  
    return warnings  

def task_warnings(unscheduled: list[DynamicTask]) -> list[ScheduleWarning]:   
    """A task that did not fit is never dropped silently. Missing a deadline is hard.""" 
    warnings = []  
    for task in unscheduled:   
        if task.deadline_day is not None:  
            warnings.append(ScheduleWarning(   
                severity="hard", kind="task_unscheduled",   
                message=f"'{task.title}' cannot be finished before its deadline.",  
            ))   
        else: 
            warnings.append(ScheduleWarning(  
                severity="soft", kind="task_unscheduled",  
                message=f"'{task.title}' did not fit in this plan.",   
            ))  
    return warnings   
 
 
 