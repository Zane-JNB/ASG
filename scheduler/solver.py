"""The CP-SAT scheduler: places tasks and sleep around fixed blocks on one multi-day slot axis."""
import math
from dataclasses import dataclass, field
from ortools.sat.python import cp_model
from scheduler.models import (
    MAX_PRIORITY, DynamicTask, FixedBlock, ProfileSettings, ScheduledItem, ScheduleWarning, SleepRule,
)
from scheduler.units import SLOTS_PER_DAY, format_hours, slot_to_time


def split_sizes(duration: int, max_session: int) -> list[int]:
    """Fewest, most even chunks that are each <= max_session."""
    n = math.ceil(duration / max_session)
    base, extra = divmod(duration, n)
    return [base + 1] * extra + [base] * (n - extra)


def chunk_sizes(task: DynamicTask) -> list[int]:
    """The sessions a task is planned in: even chunks if the student lets it split, else one block."""
    if task.splittable:
        return split_sizes(task.duration_slots, task.max_session_slots)
    return [task.duration_slots]


def merge_fixed_spans(blocks: list[FixedBlock]) -> list[tuple[int, int, bool]]:
    """Union overlapping fixed spans on the absolute slot axis. Touching spans stay separate."""
    spans = sorted((*b.span, b.buffer_before) for b in blocks)
    merged = []
    for s, e, buf in spans:
        if merged and s < merged[-1][1]:
            ps, pe, pbuf = merged[-1]
            merged[-1] = (ps, max(pe, e), pbuf or buf if s == ps else pbuf)
        else:
            merged.append((s, e, buf))
    return merged


@dataclass
class _Chunk:
    start: cp_model.IntVar
    size: int


@dataclass
class _Task:
    task: DynamicTask
    present: cp_model.IntVar
    chunks: list[_Chunk]


@dataclass
class _Night:
    start: cp_model.IntVar
    size: cp_model.IntVar
    room: int  # the most sleep this night allows (reachable_sleep)


@dataclass
class _Plan:
    """One CP-SAT model being built, shared by the build steps below."""
    num_days: int
    settings: ProfileSettings
    model: cp_model.CpModel = field(default_factory=cp_model.CpModel)
    # three no-overlap groups:
    spaced: list[cp_model.IntervalVar] = field(default_factory=list)  # tasks + buffer after, blocks, sleep + wake buffer
    flush: list[cp_model.IntervalVar] = field(default_factory=list)  # blocks a task may end right against (commutes)
    sleep: list[cp_model.IntervalVar] = field(default_factory=list)  # (flush + sleep never overlap)
    after_blocks: list[cp_model.IntervalVar] = field(default_factory=list)  # each block + buffer after it, task chunks
    costs: list[cp_model.LinearExprT] = field(default_factory=list)  # scaled like the presence bonus (_set_objective)

    @property
    def horizon(self) -> int:
        return self.num_days * SLOTS_PER_DAY


SOLVE_SECONDS = 30.0  # time limit for one plan


def build_schedule(fixed_blocks: list[FixedBlock], tasks: list[DynamicTask], num_days: int = 1,
                   sleep_rules: list[SleepRule] | None = None,
                   time_limit_seconds: float | None = SOLVE_SECONDS,
                   settings: ProfileSettings | None = None) -> tuple[list[ScheduledItem], list[DynamicTask]]:
    """(scheduled items, tasks that did not fit). With a time limit, the best plan found by then is
    used; RuntimeError if there is none."""
    items, unplaced = _schedule(fixed_blocks, tasks, num_days, sleep_rules or [], time_limit_seconds,
                                settings or ProfileSettings())
    return items, [tasks[i] for i in unplaced]


def _schedule(fixed_blocks: list[FixedBlock], tasks: list[DynamicTask], num_days: int, sleep_rules: list[SleepRule],
              time_limit_seconds: float | None, settings: ProfileSettings) -> tuple[list[ScheduledItem], list[int]]:
    """(scheduled items, positions in tasks of the ones that did not fit)."""
    _check_days(fixed_blocks, sleep_rules, num_days)
    plan = _Plan(num_days, settings)
    _add_fixed_blocks(plan, fixed_blocks)
    placed = [_add_task(plan, i, task) for i, task in enumerate(tasks)]
    nights = [_add_night(plan, rule) for rule in sleep_rules if not rule.skip]  # skipped: free for tasks
    _guard_sleep_target(plan, nights, placed)
    plan.model.AddNoOverlap(plan.spaced)
    plan.model.AddNoOverlap(plan.flush + plan.sleep)
    plan.model.AddNoOverlap(plan.after_blocks)
    _set_objective(plan, placed)
    solver = _solve(plan.model, time_limit_seconds)
    return _read_back(solver, fixed_blocks, nights, placed)


def _check_days(fixed_blocks: list[FixedBlock], sleep_rules: list[SleepRule], num_days: int) -> None:
    for block in fixed_blocks:
        if block.day >= num_days:
            raise ValueError(f"'{block.title}' is on day {block.day}, but the plan has {num_days} day(s)")
    seen = set()
    for rule in sleep_rules:
        if rule.night >= num_days:
            raise ValueError(f"sleep rule is for night {rule.night}, but the plan has {num_days} day(s)")
        if rule.night in seen:
            raise ValueError(f"two sleep rules for night {rule.night}")
        seen.add(rule.night)


def _add_fixed_blocks(plan: _Plan, blocks: list[FixedBlock]) -> None:
    """Blocks never overlap anything. After a block comes the normal buffer, which never reaches
    into the next block (two blocks may be back to back)."""
    spans = merge_fixed_spans(blocks)
    for k, (start, end, buffer_before) in enumerate(spans):
        iv = plan.model.NewFixedSizeIntervalVar(start, end - start, f"fixed_{k}")
        (plan.spaced if buffer_before else plan.flush).append(iv)
        next_start = spans[k + 1][0] if k + 1 < len(spans) else math.inf
        reach = min(end + plan.settings.buffer_slots, next_start)
        plan.after_blocks.append(plan.model.NewFixedSizeIntervalVar(start, reach - start, f"after_fixed_{k}"))


def _add_task(plan: _Plan, i: int, task: DynamicTask) -> _Task:
    """One optional interval per session: in order, before the deadline, with the buffer after each."""
    model, buffer = plan.model, plan.settings.buffer_slots
    present = model.NewBoolVar(f"present_{i}")
    chunks = []
    for j, size in enumerate(chunk_sizes(task)):
        lower = task.earliest_start if j == 0 else 0  # later chunks are bounded by the order rule instead
        start = model.NewIntVar(lower, plan.horizon - size, f"start_{i}_{j}")
        plan.spaced.append(model.NewOptionalFixedSizeIntervalVar(start, size + buffer, present, f"task_{i}_{j}"))
        plan.after_blocks.append(model.NewOptionalFixedSizeIntervalVar(start, size, present, f"chunk_{i}_{j}"))
        if task.deadline is not None:
            model.Add(start + size <= task.deadline).OnlyEnforceIf(present)
        if chunks:
            prev = chunks[-1]
            model.Add(start >= prev.start + prev.size + buffer).OnlyEnforceIf(present)
        chunks.append(_Chunk(start, size))
    if len(chunks) > 1:
        _add_days(plan, i, task, present, chunks)
    return _Task(task, present, chunks)


def _add_days(plan: _Plan, i: int, task: DynamicTask, present: cp_model.IntVar, chunks: list[_Chunk]) -> None:
    """A split task: a cost for two sessions on the same day, and its own daily cap."""
    model = plan.model
    days = []
    for j, chunk in enumerate(chunks):
        day = model.NewIntVar(0, plan.num_days - 1, f"day_{i}_{j}")
        model.AddDivisionEquality(day, chunk.start, SLOTS_PER_DAY)
        days.append(day)
    for j in range(1, len(days)):
        same_day = model.NewBoolVar(f"same_day_{i}_{j}")
        model.Add(days[j - 1] == days[j]).OnlyEnforceIf(same_day)
        model.Add(days[j - 1] != days[j]).OnlyEnforceIf(same_day.Not())
        plan.costs.append(plan.settings.same_day_penalty * same_day)
    if task.max_daily_slots is None:
        return
    for d in range(plan.num_days):
        on_day = []
        for j, (day, chunk) in enumerate(zip(days, chunks)):
            b = model.NewBoolVar(f"on_day_{i}_{d}_{j}")
            model.Add(day == d).OnlyEnforceIf(b)
            model.Add(day != d).OnlyEnforceIf(b.Not())
            on_day.append(chunk.size * b)
        model.Add(sum(on_day) <= task.max_daily_slots).OnlyEnforceIf(present)


def _add_night(plan: _Plan, rule: SleepRule) -> _Night:
    """One flexible interval: the solver picks bedtime and length. Nothing starts within the wake
    buffer after waking; a night with no room for sleep takes no time and gets no buffer (a hard
    warning, not a crash)."""
    model, s, n = plan.model, plan.settings, rule.night
    base, room = n * SLOTS_PER_DAY, reachable_sleep(rule)
    start = model.NewIntVar(base + rule.earliest_bed, base + rule.latest_bed, f"sleep_start_{n}")
    size = model.NewIntVar(0, rule.length_slots, f"sleep_size_{n}")
    if rule.latest_wake is not None:  # e.g. an early class the morning after the plan ends
        model.Add(start + size <= base + rule.latest_wake)
    slept = model.NewBoolVar(f"slept_{n}")
    model.Add(size >= 1).OnlyEnforceIf(slept)
    model.Add(size == 0).OnlyEnforceIf(slept.Not())
    ready_size = model.NewIntVar(0, rule.length_slots + s.wake_buffer_slots, f"ready_size_{n}")
    model.Add(ready_size == size + s.wake_buffer_slots * slept)
    ready_end = model.NewIntVar(base + rule.earliest_bed,
                                base + rule.latest_bed + rule.length_slots + s.wake_buffer_slots, f"ready_end_{n}")
    iv = model.NewOptionalIntervalVar(start, ready_size, ready_end, slept, f"sleep_and_ready_{n}")
    plan.spaced.append(iv)
    plan.sleep.append(iv)

    shortfall = model.NewIntVar(0, rule.min_slots, f"sleep_short_{n}")  # below the minimum
    model.Add(shortfall >= rule.min_slots - size)
    drift = model.NewIntVar(0, 2 * SLOTS_PER_DAY, f"bed_drift_{n}")  # away from the preferred bedtime
    model.AddAbsEquality(drift, start - (base + rule.preferred_bed))
    plan.costs += [s.sleep_min_penalty * shortfall,
                   s.sleep_target_penalty * (room - size),  # below what the night allows
                   s.bedtime_penalty * drift]
    return _Night(start, size, room)


def _guard_sleep_target(plan: _Plan, nights: list[_Night], placed: list[_Task]) -> None:
    """Target sleep is never traded for a task the student hasn't let use it. Sleep below
    what the nights allow costs sleep_target_penalty (_add_night). The part of it beyond the time of
    the planned tasks that may use it (each session plus a break on either side) also costs more
    per slot than any task can gain from that slot, so sleep below target never adds up to more
    than those tasks' time. Minimum sleep still outranks every task."""
    breaks = 2 * plan.settings.buffer_slots
    given_up = sum(n.room - n.size for n in nights)
    allowed = sum(p.present * sum(c.size + breaks for c in p.chunks) for p in placed if p.task.may_cut_sleep)
    beyond = plan.model.NewIntVar(0, sum(n.room for n in nights), "sleep_given_up_unallowed")
    plan.model.Add(beyond >= given_up - allowed)
    plan.costs.append(MAX_PRIORITY * plan.settings.presence_bonus * beyond)


def _set_objective(plan: _Plan, placed: list[_Task]) -> None:
    """Fit as many high-priority tasks as possible, and place them early. "Early" is averaged per
    chunk (everything else is scaled by the most chunks any task has), so a task with many late
    chunks never costs more than fitting it is worth."""
    scale = max((len(p.chunks) for p in placed), default=1)
    bonus = sum(p.task.priority * plan.settings.presence_bonus * p.present for p in placed)
    lateness = sum(p.task.priority * sum(c.start for c in p.chunks) for p in placed)
    plan.model.Maximize(scale * bonus - lateness - scale * sum(plan.costs))


def _solve(model: cp_model.CpModel, time_limit_seconds: float | None) -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    if time_limit_seconds is not None:
        solver.parameters.max_time_in_seconds = time_limit_seconds
    status = solver.Solve(model)
    if status == cp_model.UNKNOWN:  # hit the time limit before finding any feasible plan
        raise RuntimeError(
            f"No schedule found within {time_limit_seconds}s "
            "(too many tasks/constraints for the time limit -- try raising it or simplifying the plan)"
        )
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError("No valid schedule (check your fixed blocks and sleep rules)")
    return solver


def _item(title: str, abs_start: int, length: int, kind: str, saved_id: int | None = None) -> ScheduledItem:
    day, offset = divmod(abs_start, SLOTS_PER_DAY)
    return ScheduledItem(title=title, start_slot=offset, end_slot=offset + length, kind=kind, day=day,
                         saved_id=saved_id)


def _read_back(solver: cp_model.CpSolver, fixed_blocks: list[FixedBlock], nights: list[_Night],
               placed: list[_Task]) -> tuple[list[ScheduledItem], list[int]]:
    items = [ScheduledItem(title=b.title, start_slot=b.start_slot, end_slot=b.end_slot, kind="fixed", day=b.day)
             for b in fixed_blocks]
    for night in nights:
        length = solver.Value(night.size)
        if length:  # 0 = no room at all; sleep_warnings reports it
            items.append(_item("Sleep", solver.Value(night.start), length, "sleep"))
    unplaced = []
    for i, p in enumerate(placed):
        if not solver.Value(p.present):
            unplaced.append(i)
            continue
        for j, chunk in enumerate(p.chunks):
            title = p.task.title if len(p.chunks) == 1 else f"{p.task.title} ({j + 1}/{len(p.chunks)})"
            items.append(_item(title, solver.Value(chunk.start), chunk.size, "task", p.task.saved_id))
    items.sort(key=lambda i: (i.day, i.start_slot))
    return items, unplaced


def reachable_sleep(rule: SleepRule) -> int:
    """The most sleep this night allows: the target, unless latest_wake caps it (going to bed
    at the earliest bedtime)."""
    if rule.skip:
        return 0
    if rule.latest_wake is None:
        return rule.length_slots
    return max(0, min(rule.length_slots, rule.latest_wake - rule.earliest_bed))


def _night_item(rule: SleepRule, items: list[ScheduledItem]) -> ScheduledItem | None:
    """This night's sleep item: the one whose start falls inside the night's bedtime window."""
    base = rule.night * SLOTS_PER_DAY
    return next((i for i in items if i.kind == "sleep"
                 and base + rule.earliest_bed <= i.span[0] <= base + rule.latest_bed),
                None)


def sleep_warnings(sleep_rules: list[SleepRule], items: list[ScheduledItem]) -> list[ScheduleWarning]:
    """Check the finished plan against each night's rule and report anything given up."""
    warnings = []
    for rule in sorted(sleep_rules, key=lambda r: r.night):
        label = f"Night {rule.night}"
        if rule.skip:
            warnings.append(ScheduleWarning.hard("sleep_skipped", f"{label}: sleep skipped. Tasks may run through the night."))
            continue
        base = rule.night * SLOTS_PER_DAY
        found = _night_item(rule, items)
        length = 0 if found is None else found.end_slot - found.start_slot
        # an early start the morning after the plan can be the cause, not the plan itself
        if found is None:
            woke_at_cap = reachable_sleep(rule) == 0  # the cap left no room for any sleep
        else:
            woke_at_cap = found.span[1] - base == rule.latest_wake
        why = f" It has to end by then: {rule.latest_wake_reason}." if rule.latest_wake_reason and woke_at_cap else ""
        if length < rule.min_slots:
            warnings.append(ScheduleWarning.hard("sleep_short", (
                f"{label}: only {format_hours(length)} of sleep fits, below your minimum "
                f"of {format_hours(rule.min_slots)}.{why}")))
        elif length < rule.length_slots:
            warnings.append(ScheduleWarning.soft("sleep_short", (
                f"{label}: {format_hours(length)} of sleep, shorter than your "
                f"target of {format_hours(rule.length_slots)}.{why}")))
        if found is not None:
            bed = found.span[0] - base
            if bed > rule.preferred_bed:
                warnings.append(ScheduleWarning.soft("late_bedtime", (
                    f"{label}: bedtime is {slot_to_time(bed % SLOTS_PER_DAY)}, later than your "
                    f"preferred {slot_to_time(rule.preferred_bed % SLOTS_PER_DAY)}.")))
    return warnings


def task_warnings(unscheduled: list[DynamicTask]) -> list[ScheduleWarning]:
    """A task that did not fit is never dropped silently. Missing a deadline is hard."""
    return [ScheduleWarning.hard("task_unscheduled", f"'{t.title}' cannot be finished before its deadline.")
            if t.deadline_day is not None
            else ScheduleWarning.soft("task_unscheduled", f"'{t.title}' did not fit in this plan.")
            for t in unscheduled]


FIT_CHECK_SECONDS = 5.0  # time limit for one fit check among many (a search, a restore)


@dataclass(frozen=True)
class PlanFrame:
    """Everything a fit check holds fixed while the tasks change: blocks, window, sleep and settings."""
    fixed: list[FixedBlock]
    num_days: int = 1
    sleep_rules: list[SleepRule] = field(default_factory=list)
    settings: ProfileSettings = field(default_factory=ProfileSettings)

    def solve(self, tasks: list[DynamicTask], time_limit_seconds: float | None = SOLVE_SECONDS
              ) -> tuple[list[ScheduledItem], list[DynamicTask]]:
        return build_schedule(self.fixed, tasks, num_days=self.num_days, sleep_rules=self.sleep_rules,
                              time_limit_seconds=time_limit_seconds, settings=self.settings)

    def solve_all_fit(self, tasks: list[DynamicTask], time_limit_seconds: float = FIT_CHECK_SECONDS
                      ) -> tuple[list[ScheduledItem], list[ScheduleWarning]] | None:
        """(items, sleep warnings) if every task fits, else None. RuntimeError if no plan is found in time."""
        items, unscheduled = self.solve(tasks, time_limit_seconds)
        return None if unscheduled else (items, sleep_warnings(self.sleep_rules, items))

    def trial(self, tasks: list[DynamicTask], time_limit_seconds: float = FIT_CHECK_SECONDS
              ) -> tuple[list[ScheduledItem], list[ScheduleWarning]] | None:
        """solve_all_fit for one trial among many (a search or restore step): running out of time
        counts as not fitting."""
        try:
            return self.solve_all_fit(tasks, time_limit_seconds)
        except RuntimeError:
            return None

    def unplaced(self, tasks: list[DynamicTask], time_limit_seconds: float = FIT_CHECK_SECONDS) -> list[int]:
        """Positions in tasks of the ones that don't fit (e.g. due too soon)."""
        return _schedule(self.fixed, tasks, self.num_days, self.sleep_rules, time_limit_seconds, self.settings)[1]
