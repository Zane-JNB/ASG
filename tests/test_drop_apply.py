from datetime import date, datetime, time, timedelta

import pytest

from scheduler.calendar_utils import extracted_task_to_dynamic_task
from scheduler.db import (
    add_dated_block, add_extracted_task, add_plan_cut, apply_plan_changes, clear_plan_cut,
    clear_plan_cuts, connect, delete_extracted_task, get_extracted_tasks, get_or_create_student,
    get_plan_cuts,
)
from scheduler.drop_apply import apply_drop_choice
from scheduler.drop_review import _dont_add_fallback
from scheduler.dropping import propose_drops
from scheduler.models import DatedBlock, ExtractedTask, PlanAnchor
from scheduler.fit_check import build_fit_inputs, planned_tasks
from scheduler.planner import plan_from_saved

START = date(2026, 10, 5)  # a Monday


@pytest.fixture
def conn():
    return connect(":memory:")


@pytest.fixture
def sid(conn):
    return get_or_create_student(conn, "Zane")


def _task(title, hours, priority, difficulty=3, due=START):
    return ExtractedTask(title=title, date=due.isoformat(), duration_slots=int(hours * 4),
                         priority=priority, difficulty=difficulty)


def test_cuts_add_up_and_can_be_cleared(conn, sid):
    tid = add_extracted_task(conn, sid, _task("Big", 10, 2))
    add_plan_cut(conn, sid, tid, 8)
    add_plan_cut(conn, sid, tid, 4)
    assert get_plan_cuts(conn, sid) == {tid: 12}
    assert clear_plan_cut(conn, sid, tid) is True
    assert get_plan_cuts(conn, sid) == {}


def test_cut_must_be_positive_and_on_own_task(conn, sid):
    tid = add_extracted_task(conn, sid, _task("Big", 10, 2))
    other = get_or_create_student(conn, "Other")
    with pytest.raises(ValueError):
        add_plan_cut(conn, sid, tid, 0)
    with pytest.raises(ValueError):
        add_plan_cut(conn, other, tid, 4)
    assert get_plan_cuts(conn, sid) == {}


def test_deleting_a_task_removes_its_cut(conn, sid):
    tid = add_extracted_task(conn, sid, _task("Big", 10, 2))
    add_plan_cut(conn, sid, tid, 8)
    delete_extracted_task(conn, sid, tid)
    assert get_plan_cuts(conn, sid) == {}


def test_clear_plan_cuts_only_touches_this_student(conn, sid):
    other = get_or_create_student(conn, "Other")
    a = add_extracted_task(conn, sid, _task("A", 4, 2))
    b = add_extracted_task(conn, other, _task("B", 4, 2))
    add_plan_cut(conn, sid, a, 4)
    add_plan_cut(conn, other, b, 4)
    assert clear_plan_cuts(conn, sid) == 1
    assert get_plan_cuts(conn, other) == {b: 4}


def test_apply_plan_changes_is_all_or_nothing(conn, sid):
    tid = add_extracted_task(conn, sid, _task("Big", 10, 2))
    with pytest.raises(ValueError):
        apply_plan_changes(conn, sid, {tid: 4, 9999: 4}, _task("New", 2, 3))
    assert get_plan_cuts(conn, sid) == {}
    assert len(get_extracted_tasks(conn, sid)) == 1  # the new task was not saved either


def test_planned_tasks_apply_cuts_but_saved_task_is_untouched(conn, sid):
    tid = add_extracted_task(conn, sid, _task("Big", 10, 2))
    add_plan_cut(conn, sid, tid, 12)
    anchor = PlanAnchor(start_date=START, num_days=1)
    [(pid, t)] = planned_tasks(conn, sid, anchor)
    assert pid == tid and t.duration_slots == 40 - 12
    assert get_extracted_tasks(conn, sid)[0][1].duration_slots == 40  # full hours still saved


def test_fully_cut_task_is_left_out_of_the_plan(conn, sid):
    tid = add_extracted_task(conn, sid, _task("Big", 10, 2))
    add_plan_cut(conn, sid, tid, 40)
    assert planned_tasks(conn, sid, PlanAnchor(start_date=START, num_days=1)) == []


def test_fully_cut_task_gets_a_hard_warning_in_the_plan(conn, sid):
    # a full drop misses the due date, so the plan must say so on every run, not only when chosen
    tid = add_extracted_task(conn, sid, _task("Essay", 2, 5, due=START + timedelta(days=2)))
    add_extracted_task(conn, sid, _task("Other", 1, 3, due=START + timedelta(days=1)))
    add_plan_cut(conn, sid, tid, 8)
    _, _, items, warnings = plan_from_saved(conn, sid, now=datetime.combine(START, time(9, 0)),
                                            time_limit_seconds=5)
    assert "Essay" not in {i.title for i in items if i.kind == "task"}
    [w] = [w for w in warnings if w.kind == "task_dropped"]
    assert w.severity == "hard" and "'Essay'" in w.message and "2026-10-07" in w.message
    assert not any(w.kind == "task_cut" for w in warnings)


def test_partly_cut_task_gets_a_soft_note(conn, sid):
    tid = add_extracted_task(conn, sid, _task("Essay", 3, 5, due=START + timedelta(days=2)))
    add_plan_cut(conn, sid, tid, 4)
    *_, warnings = plan_from_saved(conn, sid, now=datetime.combine(START, time(9, 0)), time_limit_seconds=5)
    [w] = [w for w in warnings if w.kind == "task_cut"]
    assert w.severity == "soft" and "2h of its 3h" in w.message
    assert not any(w.kind == "task_dropped" for w in warnings)


def test_uncut_task_gets_no_cut_warning(conn, sid):
    add_extracted_task(conn, sid, _task("Essay", 3, 5, due=START + timedelta(days=2)))
    *_, warnings = plan_from_saved(conn, sid, now=datetime.combine(START, time(9, 0)), time_limit_seconds=5)
    assert not any(w.kind in ("task_cut", "task_dropped") for w in warnings)


def test_clearing_a_cut_gives_the_time_back(conn, sid):
    tid = add_extracted_task(conn, sid, _task("Big", 10, 2))
    add_plan_cut(conn, sid, tid, 16)
    clear_plan_cut(conn, sid, tid)
    [(_, t)] = planned_tasks(conn, sid, PlanAnchor(start_date=START, num_days=1))
    assert t.duration_slots == 40

def _busy_student(conn, sid):
    """One day, one class, three saved tasks due that day -- an Essay won't fit without a cut."""
    add_dated_block(conn, sid, DatedBlock(title="Class", date=START.isoformat(),
                                          start_time="08:00", end_time="12:00"))
    ids = {n: add_extracted_task(conn, sid, t) for n, t in {
        "Big": _task("Big project", 10, 2), "Reading": _task("Reading", 2, 3), "Lab": _task("Lab", 2, 4),
    }.items()}
    return ids


def _propose(conn, sid, essay, must_add=True):
    """The same inputs the add flow and plan_from_saved use (incl. the night-before sleep)."""
    fit = build_fit_inputs(conn, sid, datetime.combine(START, time(0, 0)), essay)
    report = propose_drops(fit.fixed, [t for _, t in fit.planned], fit.new_task, fit.anchor.num_days,
                           fit.sleep_rules, settings=fit.settings, must_add=must_add)
    return fit.planned, report


def test_applying_a_cut_saves_it_for_the_plan_only(conn, sid):
    _busy_student(conn, sid)
    essay = _task("Essay", 4, 4)
    planned, report = _propose(conn, sid, essay)
    assert not report.fits_already
    best = report.proposals[0]
    summary = apply_drop_choice(conn, sid, best, planned, essay)
    assert summary["new_task_id"] is not None
    assert sum(get_plan_cuts(conn, sid).values()) == sum(a.slots_lost for a in best.actions) > 0
    saved = {t.title: t.duration_slots for _, t in get_extracted_tasks(conn, sid)}
    assert saved["Big project"] == 40 and saved["Essay"] == 16  # saved hours untouched, essay saved


def test_replanning_after_apply_fits_everything_remaining(conn, sid):
    _busy_student(conn, sid)
    essay = _task("Essay", 4, 4)
    planned, report = _propose(conn, sid, essay)
    apply_drop_choice(conn, sid, report.proposals[0], planned, essay)
    anchor, fixed, items, warnings = plan_from_saved(conn, sid, 1, now=datetime.combine(START, time(0, 0)), time_limit_seconds=10)
    assert not [w for w in warnings if w.kind == "task_unscheduled"]
    assert any(i.title.startswith("Essay") for i in items)


def test_not_adding_saves_nothing(conn, sid):
    _busy_student(conn, sid)
    essay = _task("Essay", 4, 4)
    planned, report = _propose(conn, sid, essay, must_add=False)
    choice = next((p for p in report.proposals if not p.new_task_added),
                  _dont_add_fallback(extracted_task_to_dynamic_task(essay, START, 8), 9))
    summary = apply_drop_choice(conn, sid, choice, planned, essay)
    assert summary == {"cuts": {}, "new_task_id": None}
    assert get_plan_cuts(conn, sid) == {}
    assert all(t.title != "Essay" for _, t in get_extracted_tasks(conn, sid))


def test_adding_proposal_without_the_new_task_is_rejected(conn, sid):
    _busy_student(conn, sid)
    essay = _task("Essay", 4, 4)
    planned, report = _propose(conn, sid, essay)
    with pytest.raises(ValueError):
        apply_drop_choice(conn, sid, report.proposals[0], planned, None)


def test_finishing_early_lets_a_cut_task_get_its_time_back(conn, sid):
    _busy_student(conn, sid)
    essay = _task("Essay", 4, 4)
    planned, report = _propose(conn, sid, essay)
    apply_drop_choice(conn, sid, report.proposals[0], planned, essay)
    cut_id = next(iter(get_plan_cuts(conn, sid)))
    clear_plan_cut(conn, sid, cut_id)  # e.g. the Essay was finished early
    anchor = PlanAnchor(start_date=START, num_days=1)
    restored = dict(planned_tasks(conn, sid, anchor))[cut_id]
    assert restored.duration_slots == dict(planned)[cut_id].duration_slots