from datetime import date, datetime, timedelta

import pytest

from scheduler.add_with_fit import add_task_with_fit
from scheduler.db import (
    add_dated_block, add_extracted_task, add_weekly_pattern, connect, get_extracted_tasks,
    get_or_create_student, get_plan_cuts,
)
from scheduler.fit_check import build_fit_inputs, next_slot
from scheduler.models import DatedBlock, ExtractedTask, WeeklyPattern
from scheduler.planner import plan_from_saved
from scheduler.solver import build_schedule

D = date(2026, 10, 5)  # a Monday
NINE_AM = datetime(2026, 10, 5, 9, 0)


@pytest.fixture
def conn():
    return connect(":memory:")


@pytest.fixture
def sid(conn):
    return get_or_create_student(conn, "Zane")


def _task(title, hours, priority, due, difficulty=3):
    return ExtractedTask(title=title, date=due.isoformat(), duration_slots=int(hours * 4),
                         priority=priority, difficulty=difficulty)


def _class_all_day(conn, sid):
    add_dated_block(conn, sid, DatedBlock(title="Class", date=D.isoformat(),
                                          start_time="09:00", end_time="17:00"))


def _run(conn, sid, new, answers, now=NINE_AM, **kw):
    it, shown = iter(answers), []
    result = add_task_with_fit(conn, sid, new, now, ask=lambda _p: next(it), show=shown.append, **kw)
    return result, shown


def test_next_slot_rounds_up_to_the_next_quarter_hour():
    assert next_slot(datetime(2026, 10, 5, 9, 0)) == 36
    assert next_slot(datetime(2026, 10, 5, 9, 1)) == 37
    assert next_slot(datetime(2026, 10, 5, 23, 50)) == 96  # rolls into tomorrow


def test_window_reaches_the_latest_deadline_not_just_the_new_tasks(conn, sid):
    add_extracted_task(conn, sid, _task("Report", 6, 3, D + timedelta(days=3)))
    fit = build_fit_inputs(conn, sid, NINE_AM, _task("Quiz prep", 2, 5, D))
    assert fit.anchor.num_days == 4  # day 0 .. day 3


def test_window_is_capped_by_the_horizon_setting(conn, sid):
    fit = build_fit_inputs(conn, sid, NINE_AM, _task("Thesis", 5, 3, D + timedelta(days=100)))
    assert fit.anchor.num_days == 28


def test_weekly_classes_are_expanded_across_the_whole_window(conn, sid):
    add_weekly_pattern(conn, sid, WeeklyPattern(title="DS", day="Mon", start_time="09:00", end_time="11:00"))
    fit = build_fit_inputs(conn, sid, NINE_AM, _task("Thesis", 5, 3, D + timedelta(days=20)))
    assert sorted(b.day for b in fit.fixed if b.title == "DS") == [0, 7, 14]


def test_nothing_is_planned_before_now(conn, sid):
    add_extracted_task(conn, sid, _task("Report", 6, 3, D + timedelta(days=2)))
    now = datetime(2026, 10, 5, 15, 0)
    fit = build_fit_inputs(conn, sid, now, _task("Quiz prep", 2, 5, D + timedelta(days=1)))
    items, unscheduled = build_schedule(fit.fixed, [t for _, t in fit.planned] + [fit.new_task],
                                        num_days=fit.anchor.num_days, sleep_rules=fit.sleep_rules,
                                        settings=fit.settings)
    tasks = [i for i in items if i.kind == "task"]
    assert tasks and all(i.day * 96 + i.start_slot >= next_slot(now) for i in tasks)


def test_near_deadline_task_shuffles_a_later_one_instead_of_cutting_it(conn, sid):
    _class_all_day(conn, sid)
    add_extracted_task(conn, sid, _task("Report", 6, 3, D + timedelta(days=3)))
    result, shown = _run(conn, sid, _task("Quiz prep", 4, 5, D), [])  # no answers: must not ask
    assert shown == ["Added."]
    assert result["cuts"] == {} and get_plan_cuts(conn, sid) == {}


def _tight_day(conn, sid):
    _class_all_day(conn, sid)
    add_extracted_task(conn, sid, _task("Lab", 5, 4, D))
    return _task("Essay", 4, 5, D)


def test_impossible_deadline_offers_ranked_options_and_saves_the_pick(conn, sid):
    essay = _tight_day(conn, sid)
    result, shown = _run(conn, sid, essay, ["s", "1"])
    assert any("Options, best first" in l for l in shown)
    assert result["new_task_id"] is not None and sum(get_plan_cuts(conn, sid).values()) > 0
    saved = {t.title: t.duration_slots for _, t in get_extracted_tasks(conn, sid)}
    assert saved == {"Lab": 20, "Essay": 16}  # saved hours untouched


def test_choosing_not_to_add_saves_nothing(conn, sid):
    essay = _tight_day(conn, sid)
    result, shown = _run(conn, sid, essay, ["s", "2"])
    assert result["new_task_id"] is None and result["cuts"] == {}
    assert get_plan_cuts(conn, sid) == {} and len(get_extracted_tasks(conn, sid)) == 1


def test_enter_cancels_and_saves_nothing(conn, sid):
    essay = _tight_day(conn, sid)
    result, shown = _run(conn, sid, essay, ["s", "", ""])
    assert result is None and "Cancelled -- nothing saved." in shown
    assert len(get_extracted_tasks(conn, sid)) == 1


def test_late_night_task_due_today_does_not_crash(conn, sid):
    result, shown = _run(conn, sid, _task("Late", 4, 5, D), ["s", "", ""], now=datetime(2026, 10, 5, 22, 0))
    assert result is None
    assert any("would not be done by its deadline" in l for l in shown)
    assert get_extracted_tasks(conn, sid) == []


def test_overlapping_saved_blocks_stop_the_check_and_save_nothing(conn, sid):
    for title in ("A", "B"):
        add_dated_block(conn, sid, DatedBlock(title=title, date=D.isoformat(),
                                              start_time="10:00", end_time="12:00"))
    result, shown = _run(conn, sid, _task("Essay", 1, 3, D + timedelta(days=1)), [])
    assert result is None and any("Could not check the fit" in l for l in shown)
    assert get_extracted_tasks(conn, sid) == []


def test_report_moves_later_so_the_task_due_today_fits_and_nothing_is_cut(conn, sid):  # NEW
    _class_all_day(conn, sid)
    add_extracted_task(conn, sid, _task("Report", 6, 3, D + timedelta(days=3)))
    add_task_with_fit(conn, sid, _task("Quiz prep", 4, 5, D), NINE_AM,
                      ask=lambda _p: "", show=lambda _l: None)
    _, _, items, warnings = plan_from_saved(conn, sid, now=NINE_AM, time_limit_seconds=10)
    quiz = [i for i in items if i.title.startswith("Quiz prep")]
    report = [i for i in items if i.title.startswith("Report")]
    assert quiz and all(i.day == 0 for i in quiz)  # due today, done today
    assert any(i.day >= 1 for i in report)  # part of the Report was pushed to a later day
    assert all(i.day <= 3 for i in report)  # ...but never past its own deadline
    assert not [w for w in warnings if w.kind == "task_unscheduled"]
    assert get_plan_cuts(conn, sid) == {}  # shuffled, not cut

def test_manual_drop_then_save_cuts_only_what_the_student_chose(conn, sid):  # NEW
    essay = _tight_day(conn, sid)  # screen order: 1 = Lab, 2 = Essay (new)
    result, shown = _run(conn, sid, essay, ["m", "1", "d", "s"])
    lab_id = next(i for i, t in get_extracted_tasks(conn, sid) if t.title == "Lab")
    assert get_plan_cuts(conn, sid) == {lab_id: 20}
    assert result["new_task_id"] is not None


def test_manual_cancel_after_cutting_restores_the_timetable(conn, sid):  # NEW
    essay = _tight_day(conn, sid)
    result, shown = _run(conn, sid, essay, ["m", "1", "d", "", "y", ""])
    assert result is None and get_plan_cuts(conn, sid) == {}
    assert len(get_extracted_tasks(conn, sid)) == 1


def test_manual_dont_add_discards_the_cuts_made_so_far(conn, sid):  # NEW
    essay = _tight_day(conn, sid)
    result, shown = _run(conn, sid, essay, ["m", "1", "d", "n"])
    assert result["new_task_id"] is None and get_plan_cuts(conn, sid) == {}
    assert len(get_extracted_tasks(conn, sid)) == 1


def test_manual_can_shorten_the_new_task_and_keep_cutting(conn, sid):  # NEW
    essay = _tight_day(conn, sid)
    result, shown = _run(conn, sid, essay, ["m", "2", "t", "1", "1", "d", "s"])
    assert get_plan_cuts(conn, sid)[result["new_task_id"]] == 4  # 1h of the new task, plan-only

def test_automatic_applies_the_top_plan_on_y(conn, sid):  # NEW
    essay = _tight_day(conn, sid)
    result, shown = _run(conn, sid, essay, ["a", "y"])
    assert result["new_task_id"] is not None and sum(get_plan_cuts(conn, sid).values()) > 0


def test_automatic_declined_saves_nothing(conn, sid):  # NEW
    essay = _tight_day(conn, sid)
    result, shown = _run(conn, sid, essay, ["a", "", ""])
    assert result is None and get_plan_cuts(conn, sid) == {}
    assert len(get_extracted_tasks(conn, sid)) == 1

def test_declining_automatic_returns_to_the_mode_prompt(conn, sid):  # NEW
    essay = _tight_day(conn, sid)
    result, shown = _run(conn, sid, essay, ["a", "n", "m", "1", "d", "s"])
    assert "Nothing saved yet. Choose another way, or Enter to cancel." in shown
    assert result["new_task_id"] is not None


def test_the_ranked_search_runs_once_across_mode_switches(conn, sid, monkeypatch):  # NEW
    from scheduler.dropping import propose_drops
    calls = []
    monkeypatch.setattr("scheduler.add_with_fit.propose_drops",
                        lambda *a, **k: (calls.append(k.get("search", True)), propose_drops(*a, **k))[1])
    _run(conn, sid, _tight_day(conn, sid), ["a", "n", "s", "1"])
    assert calls == [False, True]  # one cheap fit check, then exactly one full search