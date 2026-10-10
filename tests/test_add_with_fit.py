from datetime import date, datetime, timedelta
import pytest
from scheduler.add_with_fit import add_task_with_fit
from scheduler.db import (
    DATED_BLOCKS, EXTRACTED_TASKS, WEEKLY_PATTERNS, connect, get_or_create_student, get_plan_cuts,
)
from scheduler.fit_check import build_fit_inputs
from scheduler.units import next_slot
from scheduler.models import DatedBlock, ExtractedTask, WeeklyPattern
from scheduler.planner import plan_from_saved
from scheduler.solver import PlanFrame, build_schedule

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
    DATED_BLOCKS.add(conn, sid, DatedBlock(title="Class", date=D.isoformat(),
                                           start_time="09:00", end_time="17:00"))


def _run(conn, sid, new, answers, now=NINE_AM, **kw):
    it, shown = iter(answers), []
    result = add_task_with_fit(conn, sid, new, now, ask=lambda _p: next(it), show=shown.append, **kw)
    return result, shown


def test_window_reaches_the_latest_deadline_not_just_the_new_tasks(conn, sid):
    EXTRACTED_TASKS.add(conn, sid, _task("Report", 6, 3, D + timedelta(days=3)))
    fit = build_fit_inputs(conn, sid, NINE_AM, _task("Quiz prep", 2, 5, D))
    assert fit.anchor.num_days == 4  # day 0 .. day 3


def test_window_is_capped_by_the_horizon_setting(conn, sid):
    fit = build_fit_inputs(conn, sid, NINE_AM, _task("Thesis", 5, 3, D + timedelta(days=100)))
    assert fit.anchor.num_days == 28


def test_weekly_classes_are_expanded_across_the_whole_window(conn, sid):
    WEEKLY_PATTERNS.add(conn, sid, WeeklyPattern(title="DS", day="Mon", start_time="09:00", end_time="11:00"))
    fit = build_fit_inputs(conn, sid, NINE_AM, _task("Thesis", 5, 3, D + timedelta(days=20)))
    assert sorted(b.day for b in fit.fixed if b.title == "DS") == [0, 7, 14]


def test_fit_inputs_hand_the_solver_one_frame_and_its_tasks(conn, sid):
    _class_all_day(conn, sid)
    report = EXTRACTED_TASKS.add(conn, sid, _task("Report", 2, 3, D + timedelta(days=1)))
    lab = EXTRACTED_TASKS.add(conn, sid, _task("Lab", 1, 4, D + timedelta(days=1)))
    fit = build_fit_inputs(conn, sid, NINE_AM, _task("Quiz prep", 1, 5, D + timedelta(days=1)))
    assert fit.frame == PlanFrame(fit.fixed, fit.anchor.num_days, fit.sleep_rules, fit.settings)
    assert [t.saved_id for t in fit.tasks] == [report, lab]
    assert [i for i, _ in fit.without([0]).planned] == [lab]
    assert len(fit.planned) == 2  # without() makes a copy


def test_nothing_is_planned_before_now(conn, sid):
    EXTRACTED_TASKS.add(conn, sid, _task("Report", 6, 3, D + timedelta(days=2)))
    now = datetime(2026, 10, 5, 15, 0)
    fit = build_fit_inputs(conn, sid, now, _task("Quiz prep", 2, 5, D + timedelta(days=1)))
    items, unscheduled = build_schedule(fit.fixed, [t for _, t in fit.planned] + [fit.new_task],
                                        num_days=fit.anchor.num_days, sleep_rules=fit.sleep_rules,
                                        settings=fit.settings)
    tasks = [i for i in items if i.kind == "task"]
    assert tasks and all(i.day * 96 + i.start_slot >= next_slot(now) for i in tasks)


@pytest.mark.parametrize("now", [datetime(2026, 10, 8, 23, 30), datetime(2026, 10, 8, 23, 50)])
def test_tonights_sleep_never_starts_before_now(conn, sid, now):
    # preferred bedtime is 23:00, but at 23:30 that time has already passed
    fit = build_fit_inputs(conn, sid, now)
    items, _ = build_schedule(fit.fixed, [], num_days=fit.anchor.num_days,
                              sleep_rules=fit.sleep_rules, settings=fit.settings)
    sleep = [i for i in items if i.kind == "sleep" and i.day * 96 + i.start_slot < 96 + 48]
    assert sleep and sleep[0].day * 96 + sleep[0].start_slot >= next_slot(now)


def test_near_deadline_task_shuffles_a_later_one_instead_of_cutting_it(conn, sid):
    _class_all_day(conn, sid)
    EXTRACTED_TASKS.add(conn, sid, _task("Report", 6, 3, D + timedelta(days=3)))
    result, shown = _run(conn, sid, _task("Quiz prep", 4, 5, D), [])  # no answers: must not ask
    assert shown == ["Added."]
    assert result.cuts == {} and get_plan_cuts(conn, sid) == {}

def _tight_day(conn, sid):
    _class_all_day(conn, sid)
    EXTRACTED_TASKS.add(conn, sid, _task("Lab", 5, 4, D))
    return _task("Essay", 4, 5, D)


def test_impossible_deadline_offers_ranked_options_and_saves_the_pick(conn, sid):
    essay = _tight_day(conn, sid)
    result, shown = _run(conn, sid, essay, ["s", "1"])
    assert any("Options, best first" in l for l in shown)
    assert result.new_task_id is not None and sum(get_plan_cuts(conn, sid).values()) > 0
    saved = {t.title: t.duration_slots for _, t in EXTRACTED_TASKS.get(conn, sid)}
    assert saved == {"Lab": 20, "Essay": 16}  # saved hours untouched


def test_choosing_not_to_add_saves_nothing(conn, sid):
    essay = _tight_day(conn, sid)
    result, shown = _run(conn, sid, essay, ["s", "2"])
    assert result.new_task_id is None and result.cuts == {}
    assert get_plan_cuts(conn, sid) == {} and len(EXTRACTED_TASKS.get(conn, sid)) == 1


def test_enter_cancels_and_saves_nothing(conn, sid):
    essay = _tight_day(conn, sid)
    result, shown = _run(conn, sid, essay, ["s", "", ""])
    assert result is None and "Cancelled -- nothing saved." in shown
    assert len(EXTRACTED_TASKS.get(conn, sid)) == 1


def test_late_night_task_due_today_does_not_crash(conn, sid):
    result, shown = _run(conn, sid, _task("Late", 4, 5, D), ["s", "", ""], now=datetime(2026, 10, 5, 22, 0))
    assert result is None
    assert any("would not be done by its deadline" in l for l in shown)
    assert EXTRACTED_TASKS.get(conn, sid) == []


def test_overlapping_saved_blocks_no_longer_stop_the_check(conn, sid):
    # both blocks are kept and the solver plans around them, so the task is still checked and added
    for title in ("A", "B"):
        DATED_BLOCKS.add(conn, sid, DatedBlock(title=title, date=D.isoformat(),
                                               start_time="10:00", end_time="12:00"))
    result, shown = _run(conn, sid, _task("Essay", 1, 3, D + timedelta(days=1)), [])
    assert result is not None and "Added." in shown
    assert not any("Could not check the fit" in l for l in shown)


def test_report_moves_later_so_the_task_due_today_fits_and_nothing_is_cut(conn, sid):
    _class_all_day(conn, sid)
    EXTRACTED_TASKS.add(conn, sid, _task("Report", 6, 3, D + timedelta(days=3)))
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

def test_manual_drop_then_save_cuts_only_what_the_student_chose(conn, sid):
    essay = _tight_day(conn, sid)  # screen order: 1 = Lab, 2 = Essay (new)
    result, shown = _run(conn, sid, essay, ["m", "1", "d", "s"])
    lab_id = next(i for i, t in EXTRACTED_TASKS.get(conn, sid) if t.title == "Lab")
    assert get_plan_cuts(conn, sid) == {lab_id: 20}
    assert result.new_task_id is not None


def test_manual_cancel_after_cutting_restores_the_timetable(conn, sid):
    essay = _tight_day(conn, sid)
    result, shown = _run(conn, sid, essay, ["m", "1", "d", "", "y", ""])
    assert result is None and get_plan_cuts(conn, sid) == {}
    assert len(EXTRACTED_TASKS.get(conn, sid)) == 1


def test_manual_dont_add_discards_the_cuts_made_so_far(conn, sid):
    essay = _tight_day(conn, sid)
    result, shown = _run(conn, sid, essay, ["m", "1", "d", "n"])
    assert result.new_task_id is None and get_plan_cuts(conn, sid) == {}
    assert len(EXTRACTED_TASKS.get(conn, sid)) == 1


def test_manual_can_shorten_the_new_task_and_keep_cutting(conn, sid):
    essay = _tight_day(conn, sid)
    result, shown = _run(conn, sid, essay, ["m", "2", "t", "1", "1", "d", "s"])
    assert get_plan_cuts(conn, sid)[result.new_task_id] == 4  # 1h of the new task, plan-only

def test_automatic_applies_the_top_plan_on_y(conn, sid):
    essay = _tight_day(conn, sid)
    result, shown = _run(conn, sid, essay, ["a", "y"])
    assert result.new_task_id is not None and sum(get_plan_cuts(conn, sid).values()) > 0


def test_automatic_declined_saves_nothing(conn, sid):
    essay = _tight_day(conn, sid)
    result, shown = _run(conn, sid, essay, ["a", "", ""])
    assert result is None and get_plan_cuts(conn, sid) == {}
    assert len(EXTRACTED_TASKS.get(conn, sid)) == 1

def test_declining_automatic_returns_to_the_mode_prompt(conn, sid):
    essay = _tight_day(conn, sid)
    result, shown = _run(conn, sid, essay, ["a", "n", "m", "1", "d", "s"])
    assert "Nothing saved yet. Choose another way, or Enter to cancel." in shown
    assert result.new_task_id is not None


def test_the_ranked_search_runs_once_across_mode_switches(conn, sid, monkeypatch):
    from scheduler.dropping import propose_drops
    calls = []
    monkeypatch.setattr("scheduler.add_with_fit.propose_drops",
                        lambda *a, **k: (calls.append(1), propose_drops(*a, **k))[1])
    _run(conn, sid, _tight_day(conn, sid), ["a", "n", "s", "1"])
    assert len(calls) == 1  # the cheap fit check is not a search; exactly one full search


def _long_day_then_early_class(conn, sid):
    DATED_BLOCKS.add(conn, sid, DatedBlock(title="Work", date=D.isoformat(), start_time="09:00", end_time="20:00"))
    DATED_BLOCKS.add(conn, sid, DatedBlock(title="Class", date=(D + timedelta(days=1)).isoformat(),
                                           start_time="06:00", end_time="08:00"))


def test_a_task_that_only_fits_by_cutting_target_sleep_is_never_added_silently(conn, sid):
    # it used to be added with a warning after "Added."; now the student is asked first
    _long_day_then_early_class(conn, sid)
    result, shown = _run(conn, sid, _task("Essay", 1.5, 3, D), [""])
    assert result is None and "Added." not in shown and EXTRACTED_TASKS.get(conn, sid) == []


def test_letting_the_new_task_use_sleep_is_saved_with_it_and_planned(conn, sid):
    _long_day_then_early_class(conn, sid)
    result, shown = _run(conn, sid, _task("Essay", 1.5, 3, D), ["a", "y"])
    assert result.cuts == {} and dict(EXTRACTED_TASKS.get(conn, sid))[result.new_task_id].may_cut_sleep
    assert "  'Essay' may use sleep below your target (saved with the task)" in shown
    _, _, items, warnings = plan_from_saved(conn, sid, now=NINE_AM)
    assert "Essay" in [i.title for i in items if i.kind == "task"]
    assert [(w.severity, w.kind) for w in warnings if w.kind.startswith("sleep")] == [("soft", "sleep_short")]


def test_easy_fit_shows_no_sleep_warning(conn, sid):
    result, shown = _run(conn, sid, _task("Essay", 1.5, 3, D + timedelta(days=3)), [])
    assert shown == ["Added."]


def test_manual_cuts_that_only_fit_with_sleep_below_target_show_it_and_save_the_leave(conn, sid):
    _long_day_then_early_class(conn, sid)
    # manual: shorten the new task (the only one, number 1) by 15 min; it still needs sleep, so
    # the check falls back to letting it use sleep below target, shown before saving
    result, shown = _run(conn, sid, _task("Essay", 1.5, 3, D), ["m", "1", "t", "0.25", "s"])
    assert "Everything fits now." in shown and any(l.startswith("  !! Sleep:") for l in shown)
    assert dict(EXTRACTED_TASKS.get(conn, sid))[result.new_task_id].may_cut_sleep
    assert "  'Essay' may use sleep below your target (saved with the task)" in shown


def test_manual_cuts_that_fit_on_their_own_keep_target_sleep(conn, sid):
    essay = _tight_day(conn, sid)
    result, _ = _run(conn, sid, essay, ["m", "1", "d", "s"])
    assert not dict(EXTRACTED_TASKS.get(conn, sid))[result.new_task_id].may_cut_sleep


def test_a_failed_full_search_returns_to_the_mode_prompt(conn, sid, monkeypatch):
    def fail_the_full_search(*a, **k):
        raise RuntimeError("No schedule found within 5.0s")
    monkeypatch.setattr("scheduler.add_with_fit.propose_drops", fail_the_full_search)
    result, shown = _run(conn, sid, _tight_day(conn, sid), ["s", ""])
    assert "Could not search for ways to make room: No schedule found within 5.0s" in shown
    assert result is None and len(EXTRACTED_TASKS.get(conn, sid)) == 1  # nothing saved


def test_a_saved_task_that_cannot_fit_anyway_does_not_block_a_new_one(conn, sid):
    EXTRACTED_TASKS.add(conn, sid, ExtractedTask(title="Lab", date=D.isoformat(), due_time="10:00",
                                                 duration_slots=32, priority=3))  # 8h, due in 1h
    result, shown = _run(conn, sid, _task("Reading", 0.5, 2, D + timedelta(days=7)), [])
    assert result.new_task_id is not None and "Added." in shown
    assert any("'Lab' can't fit in the plan even without 'Reading'" in s for s in shown)
