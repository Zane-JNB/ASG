import itertools

import pytest
from scheduler import dropping
from scheduler.dropping import (
    NEW_TASK, cut_combos, cut_options, shrink_amounts, loss_cost, propose_drops, try_cuts,
)
from scheduler.models import DynamicTask, FixedBlock, ProfileSettings, SleepRule
from scheduler.solver import PlanFrame, chunk_sizes


def busy_day():
    fixed = [FixedBlock(title="Class", start_slot=32, end_slot=48)]
    tasks = [
        DynamicTask(title="Big project", duration_slots=40, priority=2, difficulty=3, max_session_slots=8),
        DynamicTask(title="Reading", duration_slots=8, priority=3, difficulty=2),
        DynamicTask(title="Lab", duration_slots=8, priority=4, difficulty=3),
    ]
    new = DynamicTask(title="Essay", duration_slots=16, priority=4, difficulty=3)
    return fixed, tasks, new, [SleepRule(night=0)]


def _full_sort(tasks, new, settings, max_actions, need):
    """The search order the cost-ordered walk replaced: build every combination, then a stable sort by cost."""
    opts = cut_options(tasks, settings)
    new_cuts = [0] + (shrink_amounts(new.duration_slots, settings) if len(chunk_sizes(new)) == 1 else [])
    out = []
    for n in range(max_actions + 1):
        for idxs in itertools.combinations(range(len(tasks)), n):
            for pick in itertools.product(*(opts[i] for i in idxs)):
                for new_cut in new_cuts:
                    if (pick or new_cut) and sum(a.slots_lost for a in pick) + new_cut >= need:
                        cost = (sum(loss_cost(tasks[a.task_index], a.slots_lost, settings) for a in pick)
                                + loss_cost(new, new_cut, settings))
                        out.append((cost, pick, new_cut))
    return sorted(out, key=lambda c: c[0])


def _mixed_tasks():
    same = dict(duration_slots=8, priority=3, difficulty=2)  # identical tasks: many cost ties
    return [DynamicTask(title="A", **same), DynamicTask(title="B", **same),
            DynamicTask(title="Long", duration_slots=20, priority=2, difficulty=4, max_session_slots=8),
            DynamicTask(title="Due", duration_slots=6, priority=4, difficulty=1, deadline_day=0),
            DynamicTask(title="One block", duration_slots=12, priority=1, difficulty=5, splittable=False)]


@pytest.mark.parametrize("max_actions", [0, 1, 2, 3])
@pytest.mark.parametrize("need", [0, 10, 30])
@pytest.mark.parametrize("new", [DynamicTask(title="New", duration_slots=8, priority=4),
                                 DynamicTask(title="New split", duration_slots=24, priority=2, max_session_slots=8)])
def test_cut_combos_come_cheapest_first_exactly_like_a_full_sort(max_actions, need, new):
    s, tasks = ProfileSettings(), _mixed_tasks()
    assert list(cut_combos(tasks, new, s, max_actions, need)) == _full_sort(tasks, new, s, max_actions, need)


def test_cut_combos_price_each_option_once_not_every_combination(monkeypatch):
    # the old search rebuilt and priced every combination (C(40, 3) x options^3) on each batch
    s = ProfileSettings()
    tasks = [DynamicTask(title=f"T{i}", duration_slots=4 + i % 9, priority=1 + i % 5, difficulty=1 + i % 3,
                         max_session_slots=4) for i in range(40)]
    new = DynamicTask(title="New", duration_slots=6, priority=3)
    priced = []
    monkeypatch.setattr(dropping, "loss_cost", lambda *a: (priced.append(1), loss_cost(*a))[1])
    first = list(itertools.islice(cut_combos(tasks, new, s, 3, 0), 20))
    assert len(first) == 20
    options = sum(len(v) for v in cut_options(tasks, s).values()) + 1 + len(shrink_amounts(6, s))
    assert len(priced) <= options


def test_a_split_task_is_cut_by_whole_sessions():
    t = DynamicTask(title="X", duration_slots=14, priority=3, difficulty=2, max_session_slots=8)
    assert chunk_sizes(t) == [7, 7]
    assert [(a.chunks_cut, a.slots_kept) for a in cut_options([t], ProfileSettings())[0]] == [(1, 7), (2, 0)]


def test_non_splittable_task_can_only_be_dropped_whole():
    t = DynamicTask(title="X", duration_slots=6, priority=3, difficulty=2, splittable=False)
    assert chunk_sizes(t) == [6]
    assert [a.is_full_drop for a in cut_options([t], ProfileSettings())[0] if not a.shrink] == [True]


def test_deadline_and_priority_raise_the_cost_of_losing_time():
    s = ProfileSettings()
    low = DynamicTask(title="a", duration_slots=4, priority=1, difficulty=1)
    high = DynamicTask(title="b", duration_slots=4, priority=5, difficulty=1)
    due = DynamicTask(title="c", duration_slots=4, priority=1, difficulty=1, deadline_day=0)
    assert loss_cost(high, 4, s) > loss_cost(low, 4, s)
    assert loss_cost(due, 4, s) > loss_cost(low, 4, s)


def test_reports_when_everything_already_fits():
    new = DynamicTask(title="Tiny", duration_slots=2, priority=3, difficulty=1)
    report = propose_drops(PlanFrame([]), [], new)
    assert report.fits_already and report.proposals == []


def test_every_proposal_really_fits():
    fixed, tasks, new, rules = busy_day()
    report = propose_drops(PlanFrame(fixed, 1, rules), tasks, new)
    assert report.proposals
    for p in report.proposals:
        titles = {i.title.split(" (")[0] for i in p.schedule if i.kind == "task"}
        if p.added is not None:
            assert "Essay" in titles
        dropped = {a.title for a in p.actions if a.is_full_drop}
        assert not (dropped & titles)


def test_proposals_are_ranked_best_first_and_numbered():
    fixed, tasks, new, rules = busy_day()
    report = propose_drops(PlanFrame(fixed, 1, rules), tasks, new)
    scores = [p.score for p in report.proposals]
    assert scores == sorted(scores)
    assert [p.rank for p in report.proposals] == list(range(1, len(scores) + 1))
    assert len(report.proposals) <= 4


def test_partial_cut_of_a_split_task_is_offered():
    fixed, tasks, new, rules = busy_day()
    report = propose_drops(PlanFrame(fixed, 1, rules), tasks, new)
    partial = [a for p in report.proposals for a in p.actions if not a.is_full_drop]
    assert partial and partial[0].title == "Big project"


def test_no_proposal_is_a_wasteful_superset_of_another():
    fixed, tasks, new, rules = busy_day()
    report = propose_drops(PlanFrame(fixed, 1, rules), tasks, new)
    cuts = [{**{a.task_index: a.slots_lost for a in p.actions}, **({NEW_TASK: p.added.slots_cut} if p.added.slots_cut else {})}
            for p in report.proposals if p.added is not None]
    for a in cuts:
        for b in cuts:
            if a is not b:
                assert not all(b.get(i, 0) >= k for i, k in a.items())

def overloaded_by_a_cheap_new_task():
    fixed = [FixedBlock(title="Class", start_slot=32, end_slot=48)]
    tasks = [
        DynamicTask(title="Lab", duration_slots=30, priority=5, difficulty=3, splittable=False),
        DynamicTask(title="Report", duration_slots=30, priority=5, difficulty=3, splittable=False),
    ]
    return fixed, tasks, [SleepRule(night=0)]


def test_not_adding_can_be_the_best_option():
    fixed, tasks, rules = overloaded_by_a_cheap_new_task()
    new = DynamicTask(title="Optional reading", duration_slots=20, priority=1, difficulty=1)
    report = propose_drops(PlanFrame(fixed, 1, rules), tasks, new)
    assert not report.fits_already
    assert report.proposals[0].added is None
    assert report.proposals[0].actions == []


def test_must_add_removes_the_not_adding_option():
    fixed, tasks, rules = overloaded_by_a_cheap_new_task()
    new = DynamicTask(title="Optional reading", duration_slots=20, priority=1, difficulty=1)
    report = propose_drops(PlanFrame(fixed, 1, rules), tasks, new, must_add=True)
    assert all(p.added is not None for p in report.proposals)


def test_urgent_new_task_beats_not_adding():
    fixed, tasks, rules = overloaded_by_a_cheap_new_task()
    new = DynamicTask(title="Exam prep", duration_slots=20, priority=5, difficulty=4, deadline_day=0)
    report = propose_drops(PlanFrame(fixed, 1, rules), tasks, new)
    not_adding = [p for p in report.proposals if p.added is None]
    assert report.proposals[0].added is not None
    assert all(p.flags for p in not_adding)


def test_dropping_a_deadline_task_is_flagged_hard():
    fixed = [FixedBlock(title="Class", start_slot=32, end_slot=48)]
    tasks = [DynamicTask(title="Quiz", duration_slots=48, priority=2, difficulty=2, deadline_day=0, splittable=False)]
    new = DynamicTask(title="Essay", duration_slots=24, priority=5, difficulty=3, splittable=False)
    report = propose_drops(PlanFrame(fixed, 1, [SleepRule(night=0)]), tasks, new, must_add=True)
    for p in report.proposals:
        assert any("Quiz" in f for f in p.flags)


def test_search_stops_at_the_check_limit():
    fixed, tasks, new, rules = busy_day()
    report = propose_drops(PlanFrame(fixed, 1, rules), tasks, new, max_checks=2)
    assert report.checks_used <= 2
    assert report.search_exhausted is False

def test_shortening_the_new_task_is_found_when_it_fits():
    # 11 free slots; B (2) + N (10) is one slot too many. The old bound used N's whole length
    # as the amount to free, so "shorten N by 2" was skipped and only worse options were offered.
    fixed = [FixedBlock(title="Busy", start_slot=0, end_slot=85)]
    b = DynamicTask(title="B", duration_slots=2, priority=5, difficulty=2, splittable=False)
    n = DynamicTask(title="N", duration_slots=10, priority=3, difficulty=2, splittable=False)
    report = propose_drops(PlanFrame(fixed, settings=ProfileSettings(buffer_slots=0)), [b], n, must_add=True)
    best = report.proposals[0]
    assert best.actions == [] and best.added.slots_cut == 2


def test_try_cuts_scores_a_verified_plan_or_returns_none():
    fixed, tasks, new, rules = busy_day()
    frame = PlanFrame(fixed, 1, rules)
    best = next(p for p in propose_drops(frame, tasks, new).proposals if p.added is not None)
    again = try_cuts(frame, tasks, new, best.actions, best.added.slots_cut)
    assert (again.score, again.flags, again.slots_freed) == (best.score, best.flags, best.slots_freed)
    assert try_cuts(frame, tasks, new, [], 0) is None  # the plan that already failed


def _evening_frame():
    """Busy until 21:00 and up at 05:00: a task tonight only fits by sleeping below target."""
    work = FixedBlock(title="Work", start_slot=0, end_slot=84)
    rule = SleepRule(earliest_bed=84, preferred_bed=84, latest_bed=100, latest_wake=116)
    return PlanFrame([work], 1, [rule])


def test_letting_the_new_task_use_sleep_below_target_is_offered():
    quiz = DynamicTask(title="Quiz", duration_slots=1, priority=5, deadline_day=0)
    report = propose_drops(_evening_frame(), [], quiz)
    (sleepy,) = [p for p in report.proposals if p.added is not None and p.added.may_cut_sleep]
    assert sleepy.added is not None and sleepy.actions == [] and sleepy.added.slots_cut == 0
    assert sleepy.sleep_sacrificed_slots == 3
    assert not any((p.added is not None and p.added.may_cut_sleep) for p in report.proposals if p is not sleepy)


def test_sleep_below_target_is_not_offered_when_it_would_reach_below_minimum():
    essay = DynamicTask(title="Essay", duration_slots=10, priority=5, deadline_day=0, splittable=False)
    report = propose_drops(_evening_frame(), [], essay)
    assert not any((p.added is not None and p.added.may_cut_sleep) for p in report.proposals)


def test_a_proposal_says_how_the_new_task_is_added_in_one_place():
    """'Don't add' carries no cut or sleep leave; an added task carries both."""
    from scheduler.dropping import dont_add_unverified
    from scheduler.models import AddedTask
    new = DynamicTask(title="New", duration_slots=4, priority=3, deadline_day=0)
    assert dont_add_unverified(new, 1).added is None
    assert AddedTask() == AddedTask(slots_cut=0, may_cut_sleep=False)
