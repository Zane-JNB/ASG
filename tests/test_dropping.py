import pytest
from scheduler.dropping import chunk_sizes, cut_task, loss_cost, propose_drops
from scheduler.models import DynamicTask, FixedBlock, ProfileSettings, SleepRule
from scheduler.solver import build_schedule


def busy_day():
    fixed = [FixedBlock(title="Class", start_slot=32, end_slot=48)]
    tasks = [
        DynamicTask(title="Big project", duration_slots=40, priority=2, difficulty=3, max_session_slots=8),
        DynamicTask(title="Reading", duration_slots=8, priority=3, difficulty=2),
        DynamicTask(title="Lab", duration_slots=8, priority=4, difficulty=3),
    ]
    new = DynamicTask(title="Essay", duration_slots=16, priority=4, difficulty=3)
    return fixed, tasks, new, [SleepRule(night=0)]


def test_cut_task_removes_whole_sessions():
    t = DynamicTask(title="X", duration_slots=14, priority=3, difficulty=2, max_session_slots=8)
    assert chunk_sizes(t) == [7, 7]
    assert cut_task(t, 1).duration_slots == 7
    assert cut_task(t, 2) is None


def test_cut_task_leaves_original_untouched():
    t = DynamicTask(title="X", duration_slots=14, priority=3, difficulty=2, max_session_slots=8)
    cut_task(t, 1)
    assert t.duration_slots == 14


def test_non_splittable_task_can_only_be_dropped_whole():
    t = DynamicTask(title="X", duration_slots=6, priority=3, difficulty=2, splittable=False)
    assert chunk_sizes(t) == [6]
    assert cut_task(t, 1) is None


def test_deadline_and_priority_raise_the_cost_of_losing_time():
    s = ProfileSettings()
    low = DynamicTask(title="a", duration_slots=4, priority=1, difficulty=1)
    high = DynamicTask(title="b", duration_slots=4, priority=5, difficulty=1)
    due = DynamicTask(title="c", duration_slots=4, priority=1, difficulty=1, deadline_day=0)
    assert loss_cost(high, 4, s) > loss_cost(low, 4, s)
    assert loss_cost(due, 4, s) > loss_cost(low, 4, s)


def test_reports_when_everything_already_fits():
    new = DynamicTask(title="Tiny", duration_slots=2, priority=3, difficulty=1)
    report = propose_drops([], [], new, num_days=1)
    assert report.fits_already and report.proposals == []


def test_every_proposal_really_fits():
    fixed, tasks, new, rules = busy_day()
    report = propose_drops(fixed, tasks, new, 1, rules)
    assert report.proposals
    for p in report.proposals:
        titles = {i.title.split(" (")[0] for i in p.schedule if i.kind == "task"}
        if p.new_task_added:
            assert "Essay" in titles
        dropped = {a.title for a in p.actions if a.is_full_drop}
        assert not (dropped & titles)


def test_proposals_are_ranked_best_first_and_numbered():
    fixed, tasks, new, rules = busy_day()
    report = propose_drops(fixed, tasks, new, 1, rules)
    scores = [p.score for p in report.proposals]
    assert scores == sorted(scores)
    assert [p.rank for p in report.proposals] == list(range(1, len(scores) + 1))
    assert len(report.proposals) <= 4


def test_partial_cut_of_a_split_task_is_offered():
    fixed, tasks, new, rules = busy_day()
    report = propose_drops(fixed, tasks, new, 1, rules)
    partial = [a for p in report.proposals for a in p.actions if not a.is_full_drop]
    assert partial and partial[0].title == "Big project"


def test_no_proposal_is_a_wasteful_superset_of_another():
    fixed, tasks, new, rules = busy_day()
    report = propose_drops(fixed, tasks, new, 1, rules)
    cuts = [{**{a.task_index: a.slots_lost for a in p.actions}, **({-1: p.new_task_slots_cut} if p.new_task_slots_cut else {})}
            for p in report.proposals if p.new_task_added]
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
    report = propose_drops(fixed, tasks, new, 1, rules)
    assert not report.fits_already
    assert report.proposals[0].new_task_added is False
    assert report.proposals[0].actions == []


def test_must_add_removes_the_not_adding_option():
    fixed, tasks, rules = overloaded_by_a_cheap_new_task()
    new = DynamicTask(title="Optional reading", duration_slots=20, priority=1, difficulty=1)
    report = propose_drops(fixed, tasks, new, 1, rules, must_add=True)
    assert all(p.new_task_added for p in report.proposals)


def test_urgent_new_task_beats_not_adding():
    fixed, tasks, rules = overloaded_by_a_cheap_new_task()
    new = DynamicTask(title="Exam prep", duration_slots=20, priority=5, difficulty=4, deadline_day=0)
    report = propose_drops(fixed, tasks, new, 1, rules)
    not_adding = [p for p in report.proposals if not p.new_task_added]
    assert report.proposals[0].new_task_added
    assert all(p.flags for p in not_adding)


def test_dropping_a_deadline_task_is_flagged_hard():
    fixed = [FixedBlock(title="Class", start_slot=32, end_slot=48)]
    tasks = [DynamicTask(title="Quiz", duration_slots=48, priority=2, difficulty=2, deadline_day=0, splittable=False)]
    new = DynamicTask(title="Essay", duration_slots=24, priority=5, difficulty=3, splittable=False)
    report = propose_drops(fixed, tasks, new, 1, [SleepRule(night=0)], must_add=True)
    for p in report.proposals:
        assert any("Quiz" in f for f in p.flags)


def test_search_stops_at_the_check_limit():
    fixed, tasks, new, rules = busy_day()
    report = propose_drops(fixed, tasks, new, 1, rules, max_checks=2)
    assert report.checks_used <= 2
    assert report.search_exhausted is False