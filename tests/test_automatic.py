import pytest
from scheduler.drop_review import choose_automatic
from scheduler.models import DropAction, DropProposal, DropReport, DynamicTask

NEW_TASK = DynamicTask(title="Essay", duration_slots=16, priority=4, difficulty=3)


def _action():
    return DropAction(task_index=0, title="Lab", chunks_cut=1, total_chunks=2, slots_lost=8, slots_kept=12,
                      priority=3, difficulty=2, has_deadline=False)


def _proposal(rank, adds=True, sleep=0, flags=()):
    return DropProposal(rank=rank, actions=[_action()] if adds else [], new_task_added=adds, score=float(rank),
                        slots_freed=8, sleep_sacrificed_slots=sleep, flags=list(flags), schedule=[])


def _report(proposals, exhausted=True, fits=False):
    return DropReport(new_task_title="Essay", fits_already=fits, proposals=proposals,
                      checks_used=5, search_exhausted=exhausted)


def _run(report, answers, must_add=False):
    it, shown = iter(answers), []
    picked = choose_automatic(report, NEW_TASK, must_add, ask=lambda p: (shown.append(p), next(it))[1],
                              show=shown.append)
    return picked, shown


def test_shows_only_the_best_plan_and_applies_it_on_y():
    report = _report([_proposal(1), _proposal(2)])
    picked, shown = _run(report, ["y"])
    assert picked is report.proposals[0]
    assert any("<- best" in l for l in shown) and not any(l.startswith("2.") for l in shown)


@pytest.mark.parametrize("answer", ["", "n", "N", "yes please", "x"])
def test_anything_but_y_applies_nothing(answer):
    assert _run(_report([_proposal(1)]), [answer])[0] is None


def test_sleep_and_flags_are_shown_before_the_question():
    report = _report([_proposal(1, sleep=8, flags=["'Lab' would fall short of its deadline"])])
    _, shown = _run(report, ["y"])
    question = shown.index("Apply this plan? [y/N]: ")
    assert any("Sleep: 2h below target" in l for l in shown[:question])
    assert any("fall short of its deadline" in l for l in shown[:question])


def test_search_limit_note_appears_only_when_the_search_was_cut_short():
    assert any("search stopped" in l for l in _run(_report([_proposal(1)], exhausted=False), [""])[1])
    assert not any("search stopped" in l for l in _run(_report([_proposal(1)]), [""])[1])


def test_must_add_never_picks_a_dont_add_plan():
    report = _report([_proposal(1, adds=False), _proposal(2)])
    picked, _ = _run(report, ["y"], must_add=True)
    assert picked is report.proposals[1] and picked.new_task_added


def test_no_plan_at_all_says_so_and_does_not_ask():
    picked, shown = _run(_report([]), [], must_add=True)
    assert picked is None and shown == ["No combination of cuts found that fits 'Essay'."]


def test_only_a_dont_add_option_left_is_offered_as_the_best_plan():
    picked, shown = _run(_report([]), ["y"])
    assert picked is not None and not picked.new_task_added
    assert any("Don't add 'Essay'" in l for l in shown)


def test_nothing_to_choose_if_it_already_fits():
    with pytest.raises(ValueError):
        choose_automatic(_report([], fits=True), NEW_TASK)