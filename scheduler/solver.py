import math
from ortools.sat.python import cp_model
from scheduler.models import (
    DynamicTask,Exam, FixedBlock, MINUTES_PER_SLOT, ProfileSettings, ScheduledItem, ScheduleWarning,
    SleepRule, SLOTS_PER_DAY, slot_to_time, StudyPlanRule, )

#Splits tasks into chunks no larger than the student's preference.
def split_sizes(duration: int, max_session: int) -> list[int]:
    """Fewest, most even chunks that are each <= max_session."""
    n = math.ceil(duration / max_session)
    base, extra = divmod(duration, n)
    return [base + 1] * extra + [base] * (n - extra)

def hours(slots: int) -> str:  
    return f"{slots * MINUTES_PER_SLOT / 60:.2g}h"  

def merge_fixed_spans(blocks: list[FixedBlock]) -> list[tuple[int, int, bool]]:   
    """Union overlapping fixed spans on the absolute slot axis. Touching spans stay separate."""
    spans = sorted((b.day * SLOTS_PER_DAY + b.start_slot,
                    b.day * SLOTS_PER_DAY + b.end_slot, b.buffer_before) for b in blocks)
    merged = []
    for s, e, buf in spans:
        if merged and s < merged[-1][1]:
            ps, pe, pbuf = merged[-1]
            merged[-1] = (ps, max(pe, e), pbuf or buf if s == ps else pbuf)   
        else:
            merged.append((s, e, buf))
    return merged

def build_schedule(fixed_blocks: list[FixedBlock], tasks: list[DynamicTask],
                buffer_slots: int | None = None , num_days: int = 1,
                sleep_rules: list[SleepRule] | None = None,
                time_limit_seconds: float | None = 30.0, #Optimal->Use it. Time up, but feasible plan found. Else RunTime Error.
                settings: ProfileSettings | None = None): 
    
    settings = settings or ProfileSettings()
    model = cp_model.CpModel()
    buffer_slots = settings.buffer_slots if buffer_slots is None else buffer_slots
    intervals = []

    fixed_block_bounds = [] #(abs_start, abs_end) per fixed block, for the buffer-after rule below
    flush_intervals = []  #  spans a task may end flush against (buffer_before=False)
    sleep_intervals = []   
    for block in fixed_blocks:
        if block.day >= num_days:
            raise ValueError(
                f"'{block.title}' is on day {block.day}, but the plan has {num_days} day(s)"
            )

    for k, (start, end, buffer_before) in enumerate(merge_fixed_spans(fixed_blocks)):   
        iv = model.NewFixedSizeIntervalVar(start, end - start, f"fixed_{k}")   
        (intervals if buffer_before else flush_intervals).append(iv)   
        fixed_block_bounds.append((start, end))   
 
    # tasks: each becomes one or more chunks the solver places
    placed = []  # (task, is_present, [(start_expr, size), ...])
    spread_cost = []  #  penalty for two sessions of the same task on the same day
    for i, task in enumerate(tasks):
        is_present = model.NewBoolVar(f"present_{i}")
 
        if task.splittable:
            sizes = split_sizes(task.duration_slots, task.max_session_slots)
        else:
            sizes = [task.duration_slots]
 
        chunks = []
        chunk_days = []
        earliest = 0
        if task.earliest_start_day is not None:
            earliest = task.earliest_start_day * SLOTS_PER_DAY + task.earliest_start_slot
        for j, size in enumerate(sizes):
            lower = earliest if j == 0 else 0  # later chunks are bounded by the ordering constraint instead
            start = model.NewIntVar(lower, num_days * SLOTS_PER_DAY - size, f"start_{i}_{j}")
 
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

                prev_day = chunk_days[-1][0]
                # model.AddDivisionEquality(prev_day, prev_start, SLOTS_PER_DAY)
                this_day = model.NewIntVar(0, num_days - 1, f"day_{i}_{j}")
                model.AddDivisionEquality(this_day, start, SLOTS_PER_DAY)
                same_day = model.NewBoolVar(f"same_day_{i}_{j}") 
                model.Add(prev_day == this_day).OnlyEnforceIf(same_day) 
                model.Add(prev_day != this_day).OnlyEnforceIf(same_day.Not())
                spread_cost.append(settings.same_day_penalty * same_day)
            else:
                this_day = model.NewIntVar(0, num_days - 1, f"day_{i}_{j}")  # first chunk's day
                model.AddDivisionEquality(this_day, start, SLOTS_PER_DAY)

            chunk_days.append((this_day, size))
            chunks.append((start, size))

            
        if task.max_daily_slots is not None and len(chunk_days) > 1:  # : cap this task's per-day total
            for d in range(num_days):
                on_day = []
                for day_var, size in chunk_days:
                    b = model.NewBoolVar(f"on_day_{i}_{d}_{len(on_day)}")
                    model.Add(day_var == d).OnlyEnforceIf(b)
                    model.Add(day_var != d).OnlyEnforceIf(b.Not())
                    on_day.append((b, size))
                model.Add(
                    sum(size * b for b, size in on_day) <= task.max_daily_slots
                ).OnlyEnforceIf(is_present)
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
        sleep_iv = model.NewIntervalVar(start, size, end, f"sleep_{rule.night}")   
        intervals.append(sleep_iv)   
        sleep_intervals.append(sleep_iv)      
 
        # how far below the minimum (0 if the minimum is met)
        shortfall = model.NewIntVar(0, rule.min_slots, f"sleep_short_{rule.night}")   
        model.Add(shortfall >= rule.min_slots - size)  
        # how far the bedtime is from the preferred one
        drift = model.NewIntVar(0, 2 * SLOTS_PER_DAY, f"bed_drift_{rule.night}")   
        model.AddAbsEquality(drift, start - (base + rule.preferred_bed))   
 
        sleep_cost.append(settings.sleep_min_penalty * shortfall)  
        sleep_cost.append(settings.sleep_target_penalty * (rule.length_slots - size))   
        sleep_cost.append(settings.bedtime_penalty * drift)   
        sleep_placed.append((rule, start, size))   
 
    for bi, (bs, be) in enumerate(fixed_block_bounds):
        for i, (task, is_present, chunks) in enumerate(placed):
            for j, (start, size) in enumerate(chunks):
                after_block = model.NewBoolVar(f"after_block_{bi}_{i}_{j}")
                model.Add(start >= be + buffer_slots).OnlyEnforceIf([is_present, after_block])
                model.Add(start + size <= bs).OnlyEnforceIf([is_present, after_block.Not()])
    model.AddNoOverlap(intervals)
    model.AddNoOverlap(flush_intervals + sleep_intervals)
 
    # goal: fit as many high-priority tasks as possible, and place them early.
    # "early" is averaged per chunk (everything else is scaled by the most chunks any task has),
    # so a task with many late chunks never costs more than fitting it is worth
    scale = max([len(chunks) for _, _, chunks in placed], default=1)
    model.Maximize(
        scale * sum(task.priority * settings.presence_bonus * present for task, present, _ in placed)
        - sum(task.priority * sum(start for start, _ in chunks) for task, _, chunks in placed)
        - scale * sum(sleep_cost)
        - scale * sum(spread_cost)
    )
 
    solver = cp_model.CpSolver()
    if time_limit_seconds is not None: 
        solver.parameters.max_time_in_seconds = time_limit_seconds 
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        if status == cp_model.UNKNOWN:  #hit the time limit before finding any feasible plan
            raise RuntimeError(  
                f"No schedule found within {time_limit_seconds}s "  
                "(too many tasks/constraints for the time limit -- try raising it or simplifying the plan)"  
            )  
        raise RuntimeError("No valid schedule (check your fixed blocks and sleep rules)")
 
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
                    saved_id=task.saved_id,
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

def generate_study_tasks(exams: list[Exam], rule: StudyPlanRule |
                        None = None, max_session_slots: int | None = None,
                        settings: ProfileSettings | None = None) -> list[DynamicTask]:
    """Turn each exam into a study DynamicTask, sized and windowed by its difficulty band.
    
    Sessions are capped at DEFAULT_MAX_SESSION_SLOTS (2h) unless max_session_slots is given
    explicitly -- band.max_hours_per_day is the daily study target, not a session-length cap.
    """
    rule = rule or StudyPlanRule()
    settings = settings or ProfileSettings()
    session_cap = max_session_slots or settings.default_max_session_slots  
    tasks = []

    for exam in exams:
        band = rule.band_for(exam.difficulty)
        slots_per_hour = 60 // MINUTES_PER_SLOT

        duration_slots = round(band.min_hours_per_day * band.days_before * slots_per_hour)
        max_session_slots = round(band.max_hours_per_day * slots_per_hour)

        earliest_day = max(0, exam.day - band.days_before)

        tasks.append(DynamicTask(
            title=f"Study: {exam.title}",
            duration_slots=duration_slots,
            priority=exam.priority,
            difficulty=exam.difficulty,
            splittable=True,
            max_session_slots=session_cap,
            max_daily_slots=round(band.max_hours_per_day * slots_per_hour),
            deadline_day=exam.day,
            deadline_slot=exam.slot,
            earliest_start_day=earliest_day,
            earliest_start_slot=0,
        ))
    return tasks
 
 
 