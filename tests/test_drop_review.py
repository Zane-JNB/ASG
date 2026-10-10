import pytest
from scheduler.make_room import choose_drop_proposal, describe_proposal
from scheduler.dropping import manual_actions, propose_drops
from scheduler.models import DropProposal, DynamicTask, FixedBlock, SleepRule
from scheduler.solver import PlanFrame
from tests.test_dropping import busy_day

def _busy():
    fixed = [FixedBlock(title="Class", start_slot=32, end_slot=48)]
    tasks = [
        DynamicTask(title="Big project", duration_slots=40, priority=2, difficulty=3, max_session_slots=8),
        DynamicTask(title="Reading", duration_slots=8, priority=3, difficulty=2),
        DynamicTask(title="Lab", duration_slots=8, priority=4, difficulty=3),
    ]
    new = DynamicTask(title="Essay", duration_slots=16, priority=4, difficulty=3)
    return propose_drops(PlanFrame(fixed, 1, [SleepRule(night=0)]), tasks, new), new


def _overloaded():
    """Existing tasks already overflow, so 'don't add' cannot be verified."""
    fixed = [FixedBlock(title="Class", start_slot=32, end_slot=48)]
    tasks = [DynamicTask(title="A", duration_slots=40, priority=3, difficulty=3, splittable=False),
             DynamicTask(title="B", duration_slots=40, priority=3, difficulty=3, splittable=False)]
    new = DynamicTask(title="Essay", duration_slots=16, priority=4, difficulty=3, splittable=False)
    return propose_drops(PlanFrame(fixed, 1, [SleepRule(night=0)]), tasks, new), new


def _run(report, new, answers, **kw):
    it, shown = iter(answers), []
    picked = choose_drop_proposal(report, new, ask=lambda _p: next(it), show=shown.append, **kw)
    return picked, shown


def test_options_are_listed_numbered_best_first():
    report, new = _busy()
    _, shown = _run(report, new, [""])
    numbered = [l for l in shown if l.split(".")[0].isdigit()]
    assert [l.split(".")[0] for l in numbered] == [str(i) for i in range(1, len(numbered) + 1)]
    assert "<- best" in numbered[0] and all("<- best" not in l for l in numbered[1:])


def test_picking_a_number_returns_that_proposal():
    report, new = _busy()
    picked, _ = _run(report, new, ["2"])
    assert picked is report.proposals[1]


def test_enter_cancels():
    report, new = _busy()
    assert _run(report, new, [""])[0] is None


def test_bad_input_reasks():
    report, new = _busy()
    picked, shown = _run(report, new, ["x", "0", "99", "1"])
    assert picked is report.proposals[0]
    assert sum("Enter a number between" in l for l in shown) == 3


def test_dont_add_is_always_offered_even_when_unverified():
    report, new = _overloaded()
    assert not any(not p.new_task_added for p in report.proposals)
    picked, shown = _run(report, new, ["9", "%d" % (len(report.proposals) + 1)])
    assert picked.new_task_added is False and picked.actions == []
    assert any("Not verified" in l for l in shown)


def test_dont_add_is_not_offered_when_must_add():
    report, new = _busy()
    _, shown = _run(report, new, [""], must_add=True)
    assert not any("Don't add" in l for l in shown)


def test_verified_dont_add_keeps_its_ranked_position():
    fixed = [FixedBlock(title="Class", start_slot=32, end_slot=48)]
    tasks = [DynamicTask(title="Lab", duration_slots=30, priority=5, difficulty=3, splittable=False),
             DynamicTask(title="Report", duration_slots=30, priority=5, difficulty=3, splittable=False)]
    new = DynamicTask(title="Optional reading", duration_slots=20, priority=1, difficulty=1)
    report = propose_drops(PlanFrame(fixed, 1, [SleepRule(night=0)]), tasks, new)
    picked, shown = _run(report, new, ["1"])
    assert picked.new_task_added is False
    assert sum("Don't add" in l for l in shown) == 1


def test_fits_already_is_rejected():
    new = DynamicTask(title="Tiny", duration_slots=2, priority=3, difficulty=1)
    report = propose_drops(PlanFrame([]), [], new)
    with pytest.raises(ValueError):
        choose_drop_proposal(report, new, ask=lambda _p: "", show=lambda _l: None)


def test_describe_shows_cut_details_and_flags():
    report, new = _busy()
    cut = next(p for p in report.proposals if p.new_task_added)
    text = "\n".join(describe_proposal(2, cut, new))
    assert "sessions of 'Big project'" in text and "Adds 'Essay'" in text and "Sleep:" in text


def test_search_stopped_note_is_shown():
    fixed = [FixedBlock(title="Class", start_slot=32, end_slot=48)]
    tasks = [DynamicTask(title="Big project", duration_slots=40, priority=2, difficulty=3, max_session_slots=8),
             DynamicTask(title="Reading", duration_slots=8, priority=3, difficulty=2)]
    new = DynamicTask(title="Essay", duration_slots=24, priority=4, difficulty=3)
    report = propose_drops(PlanFrame(fixed, 1, [SleepRule(night=0)]), tasks, new, max_checks=2)
    _, shown = _run(report, new, [""])
    assert any("search stopped" in l for l in shown)

def test_higher_priority_new_task_beats_not_adding_when_cutting_a_lower_priority_one():   
    fixed, tasks, new, rules = busy_day()  # Essay is priority 4, Big project priority 2
    report = propose_drops(PlanFrame(fixed, 1, rules), tasks, new)
    assert report.proposals[0].new_task_added
    assert [a.title for a in report.proposals[0].actions] == ["Big project"]

def _manual_line(task, slots_cut):
    proposal = DropProposal(actions=manual_actions([task], {0: slots_cut}), new_task_added=True, score=0,
                            slots_freed=slots_cut, sleep_sacrificed_slots=0, flags=[], schedule=[])
    new = DynamicTask(title="New", duration_slots=4, priority=3)
    return describe_proposal(1, proposal, new)[1]


@pytest.mark.parametrize("slots_cut, expected", [
    (4, "Shorten 'Essay' by 1h (4h -> 3h, still 2 sessions)"),
    (10, "Shorten 'Essay' by 2h 30m (4h -> 1h 30m, now one block)"),
    (16, "Drop 'Essay' entirely (-4h)"),
])
def test_a_manual_cut_of_a_split_task_says_how_many_sessions_are_left(slots_cut, expected):
    # #16: a manual cut always read "still one block", even for a task planned in two sessions
    essay = DynamicTask(title="Essay", duration_slots=16, priority=3, max_session_slots=8)
    assert _manual_line(essay, slots_cut).startswith(f"   - {expected} [")


def test_a_manual_cut_of_a_one_block_task_is_still_one_block():
    lab = DynamicTask(title="Lab", duration_slots=8, priority=3, splittable=False)
    assert "Shorten 'Lab' by 1h (2h -> 1h, still one block)" in _manual_line(lab, 4)
