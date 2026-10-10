from datetime import date, datetime, time, timedelta

import pytest

from scheduler.completion import due_checkins, finish_task, record_plan, run_checkin
from scheduler.db import (
    DATED_BLOCKS, EXTRACTED_TASKS, add_plan_cut, connect, get_or_create_student, get_plan_cuts,
    load_settings, reduce_plan_cut, save_settings,
)
from scheduler.fit_check import build_fit_inputs
from scheduler.models import DatedBlock, DynamicTask, ExtractedTask, ProfileSettings
from scheduler.planner import plan_from_saved
from scheduler.restore import plan_restores
from scheduler.task_filter import describe_reminders, task_matters, wants_reminder
from scheduler.task_manager import run_menu

D = date(2026, 10, 5)  # a Monday
NINE = datetime(2026, 10, 5, 9, 0)


def _planned(conn, sid, day):
    """[(saved task id, task)] as planned from midnight that day, plan cuts applied."""
    return build_fit_inputs(conn, sid, datetime.combine(day, time())).planned


def scripted(answers):
    it, shown = iter(answers), []
    return (lambda _p: next(it)), shown


@pytest.fixture
def conn():
    return connect(":memory:")


@pytest.fixture
def sid(conn):
    return get_or_create_student(conn, "Zane")


def _task(title, hours=2, priority=3, difficulty=3, due=D + timedelta(days=1), **kw):
    return ExtractedTask(title=title, date=due.isoformat(), duration_slots=int(hours * 4),
                         priority=priority, difficulty=difficulty, **kw)


def _set(conn, sid, **kw):
    save_settings(conn, sid, load_settings(conn, sid).model_copy(update=kw))


# ---------- the reusable filter ----------
@pytest.mark.parametrize("min_d, min_p, diff, prio, expected", [
    (None, None, 1, 1, True),   # no thresholds: everything matters
    (4, None, 4, 1, True), (4, None, 3, 5, False),   # difficulty only
    (None, 5, 1, 5, True), (None, 5, 5, 4, False),   # priority only
    (3, 5, 3, 1, True), (3, 5, 1, 5, True), (3, 5, 2, 4, False),   # either one qualifies (OR)
])
def test_task_matters_is_either_threshold(min_d, min_p, diff, prio, expected):
    t = _task("x", difficulty=diff, priority=prio)
    assert task_matters(t, min_d, min_p) is expected


def test_filter_also_works_on_planned_tasks():
    dyn = DynamicTask(title="x", duration_slots=4, priority=5, difficulty=1)
    assert task_matters(dyn, None, 5) and not task_matters(dyn, 4, None)


def test_reminders_can_be_switched_off_and_described():
    s = ProfileSettings(reminders_enabled=False)
    assert not wants_reminder(_task("x"), s) and describe_reminders(s) == "Reminders are off."
    assert describe_reminders(ProfileSettings()) == "Reminders are on for all tasks."
    assert describe_reminders(ProfileSettings(reminder_min_difficulty=4)) == "Reminders are on for difficulty 4+ tasks."
    assert describe_reminders(ProfileSettings(reminder_min_difficulty=3, reminder_min_priority=5)) \
        == "Reminders are on for difficulty 3+ or priority 5+ tasks."


def test_reminder_thresholds_must_be_1_to_5():
    with pytest.raises(ValueError):
        ProfileSettings(reminder_min_difficulty=6)


# ---------- marking done ----------
def test_done_task_is_kept_as_history_but_never_planned(conn, sid):
    tid = EXTRACTED_TASKS.add(conn, sid, _task("Essay"))
    finish_task(conn, sid, tid, NINE, ask=lambda _p: "", show=lambda _l: None)
    saved = dict(EXTRACTED_TASKS.get(conn, sid))[tid]
    assert saved.completed_at == "2026-10-05T09:00"
    assert _planned(conn, sid, D) == []


def test_finishing_clears_its_own_cut_and_rejects_bad_ids(conn, sid):
    tid = EXTRACTED_TASKS.add(conn, sid, _task("Essay", hours=4))
    add_plan_cut(conn, sid, tid, 8)
    finish_task(conn, sid, tid, NINE, ask=lambda _p: "", show=lambda _l: None)
    assert get_plan_cuts(conn, sid) == {}
    with pytest.raises(ValueError):
        finish_task(conn, sid, tid, NINE)  # already done
    with pytest.raises(ValueError):
        finish_task(conn, sid, 999, NINE)


def _tight_day(conn, sid):
    """Class 09-17. Lab (5h) was cut by 13 slots to make room for the Essay (4h), both due today."""
    DATED_BLOCKS.add(conn, sid, DatedBlock(title="Class", date=D.isoformat(), start_time="09:00", end_time="17:00"))
    lab = EXTRACTED_TASKS.add(conn, sid, _task("Lab", hours=5, priority=4, due=D))
    essay = EXTRACTED_TASKS.add(conn, sid, _task("Essay", hours=4, priority=5, due=D))
    add_plan_cut(conn, sid, lab, 13)
    return lab, essay


def test_finishing_a_task_gives_cut_hours_back_after_asking(conn, sid):
    lab, essay = _tight_day(conn, sid)
    shown = []
    result = finish_task(conn, sid, essay, NINE, ask=lambda _p: "", show=shown.append)  # Enter = yes
    assert result == {lab: 13}
    assert get_plan_cuts(conn, sid) == {}
    assert any("'Lab' +3h 15m" in l for l in shown)


def test_declining_the_restore_keeps_the_cut(conn, sid):
    lab, essay = _tight_day(conn, sid)
    result = finish_task(conn, sid, essay, NINE, ask=lambda _p: "n", show=lambda _l: None)
    assert result == {} and get_plan_cuts(conn, sid) == {lab: 13}


def test_only_what_still_fits_is_restored(conn, sid):
    lab, essay = _tight_day(conn, sid)
    EXTRACTED_TASKS.add(conn, sid, _task("Report", hours=2.5, priority=2, due=D))  # takes some of the freed room
    finish_task(conn, sid, essay, NINE, ask=lambda _p: "n", show=lambda _l: None)  # frees the Essay's time
    amount = plan_restores(conn, sid, NINE)[lab]
    assert 0 < amount < 13  # only part of the cut fits alongside the Report
    reduce_plan_cut(conn, sid, lab, amount)
    _, _, _, warnings = plan_from_saved(conn, sid, now=NINE, time_limit_seconds=10)
    assert not [w for w in warnings if w.kind == "task_unscheduled"]  # the restored amount really fits


def test_partial_restore_leaves_the_rest_of_the_cut(conn, sid):
    lab, essay = _tight_day(conn, sid)
    EXTRACTED_TASKS.add(conn, sid, _task("Report", hours=2.5, priority=2, due=D))
    finish_task(conn, sid, essay, NINE, ask=lambda _p: "", show=lambda _l: None)  # Enter = restore
    assert 0 < get_plan_cuts(conn, sid)[lab] < 13


def test_nothing_to_restore_when_there_are_no_cuts(conn, sid):
    EXTRACTED_TASKS.add(conn, sid, _task("Essay"))
    assert plan_restores(conn, sid, NINE) == {}


def test_fully_cut_task_is_checked_up_to_its_own_due_date(conn, sid):
    # the other open task is due today, so the plan window without 'Far' is one day long;
    # today is full, but 'Far' is due in a week, so all of it can come back
    DATED_BLOCKS.add(conn, sid, DatedBlock(title="Class", date=D.isoformat(), start_time="10:00", end_time="21:00"))
    far = EXTRACTED_TASKS.add(conn, sid, _task("Far", hours=2, due=D + timedelta(days=7)))
    EXTRACTED_TASKS.add(conn, sid, _task("Soon", hours=1, due=D))
    add_plan_cut(conn, sid, far, 8)
    assert plan_restores(conn, sid, NINE) == {far: 8}


def test_restore_window_stays_within_the_plan_horizon(conn, sid):
    _set(conn, sid, plan_horizon_max_days=3)
    far = EXTRACTED_TASKS.add(conn, sid, _task("Far", hours=2, due=D + timedelta(days=40)))
    add_plan_cut(conn, sid, far, 8)
    assert plan_restores(conn, sid, NINE) == {far: 8}  # fits inside the capped 3-day window


def test_a_task_that_cannot_fit_anyway_does_not_block_restores(conn, sid):
    # #18: 'Lab' (8h, due in an hour) never fits, so every restore check used to fail
    EXTRACTED_TASKS.add(conn, sid, ExtractedTask(title="Lab", date=D.isoformat(), due_time="10:00",
                                                 duration_slots=32, priority=3))
    far = EXTRACTED_TASKS.add(conn, sid, _task("Far", hours=2, due=D + timedelta(days=7)))
    add_plan_cut(conn, sid, far, 8)
    assert plan_restores(conn, sid, NINE) == {far: 8}


def test_a_cut_task_that_cannot_fit_anyway_gets_no_time_back(conn, sid):
    lab = EXTRACTED_TASKS.add(conn, sid, ExtractedTask(title="Lab", date=D.isoformat(), due_time="10:00",
                                                       duration_slots=32, priority=3))
    add_plan_cut(conn, sid, lab, 4)  # still 7h, due in an hour: even a little more can't be planned
    assert plan_restores(conn, sid, NINE) == {}


def test_higher_priority_cut_task_is_restored_first(conn, sid):
    DATED_BLOCKS.add(conn, sid, DatedBlock(title="Class", date=D.isoformat(), start_time="09:00", end_time="17:00"))
    low = EXTRACTED_TASKS.add(conn, sid, _task("Low", hours=4, priority=2, due=D))
    high = EXTRACTED_TASKS.add(conn, sid, _task("High", hours=4, priority=5, due=D))
    add_plan_cut(conn, sid, low, 16)
    add_plan_cut(conn, sid, high, 16)  # both fully cut; the evening cannot hold both
    restores = plan_restores(conn, sid, NINE)
    assert restores.get(high, 0) >= restores.get(low, 0) and restores.get(high, 0) > 0


# ---------- remembering sessions, then asking about them ----------
def _planned_student(conn, sid, **task_kw):
    tid = EXTRACTED_TASKS.add(conn, sid, _task("Essay", hours=3, due=D + timedelta(days=1), **task_kw))
    anchor, _, items, _ = plan_from_saved(conn, sid, now=NINE, time_limit_seconds=10)
    return tid, anchor, items


def test_record_plan_maps_split_sessions_back_to_the_saved_task(conn, sid):
    tid, anchor, items = _planned_student(conn, sid)
    n = record_plan(conn, sid, anchor, items, NINE)
    assert n == len([i for i in items if i.kind == "task"]) and n >= 2  # 3h is split into sessions
    later = NINE + timedelta(days=3)
    assert [c.task_id for c in due_checkins(conn, sid, later)] == [tid]  # one entry per task, not per session


def test_record_plan_keeps_same_titled_tasks_apart(conn, sid):
    first = EXTRACTED_TASKS.add(conn, sid, _task("Homework", due=D + timedelta(days=1)))
    second = EXTRACTED_TASKS.add(conn, sid, _task("Homework", due=D + timedelta(days=5)))
    anchor, _, items, _ = plan_from_saved(conn, sid, now=NINE, time_limit_seconds=10)
    record_plan(conn, sid, anchor, items, NINE)
    later = NINE + timedelta(days=7)
    assert sorted(c.task_id for c in due_checkins(conn, sid, later)) == sorted([first, second])


def test_nothing_is_due_before_the_sessions_end(conn, sid):
    _, anchor, items = _planned_student(conn, sid)
    record_plan(conn, sid, anchor, items, NINE)
    assert due_checkins(conn, sid, NINE) == []


def test_replanning_replaces_future_sessions_but_keeps_ended_ones(conn, sid):
    tid, anchor, items = _planned_student(conn, sid)
    record_plan(conn, sid, anchor, items, NINE)
    mid = max(datetime.combine(D, datetime.min.time()) + timedelta(minutes=15 * (i.day * 96 + i.end_slot))
              for i in items if i.kind == "task") - timedelta(minutes=1)
    anchor2, _, items2, _ = plan_from_saved(conn, sid, now=mid, time_limit_seconds=10)
    record_plan(conn, sid, anchor2, items2, mid)
    assert [c.task_id for c in due_checkins(conn, sid, mid)] == [tid]  # earlier sessions still await an answer


def test_only_matching_open_tasks_get_reminders(conn, sid):
    easy = EXTRACTED_TASKS.add(conn, sid, _task("Easy", difficulty=2, priority=2))
    hard = EXTRACTED_TASKS.add(conn, sid, _task("Hard", difficulty=5, priority=2))
    vip = EXTRACTED_TASKS.add(conn, sid, _task("Vip", difficulty=1, priority=5))
    anchor, _, items, _ = plan_from_saved(conn, sid, now=NINE, time_limit_seconds=10)
    record_plan(conn, sid, anchor, items, NINE)
    later = NINE + timedelta(days=3)
    ids = lambda: sorted(c.task_id for c in due_checkins(conn, sid, later))
    assert ids() == sorted([easy, hard, vip])                      # default: every task
    _set(conn, sid, reminder_min_difficulty=4)
    assert ids() == [hard]                                          # difficulty 4+ only
    _set(conn, sid, reminder_min_difficulty=None, reminder_min_priority=5)
    assert ids() == [vip]                                           # priority 5 only
    _set(conn, sid, reminder_min_difficulty=4, reminder_min_priority=5)
    assert ids() == sorted([hard, vip])                             # either qualifies
    _set(conn, sid, reminders_enabled=False)
    assert ids() == []


def test_check_in_yes_finishes_the_task_and_no_asks_only_once(conn, sid):
    a = EXTRACTED_TASKS.add(conn, sid, _task("Alpha"))
    b = EXTRACTED_TASKS.add(conn, sid, _task("Beta"))
    anchor, _, items, _ = plan_from_saved(conn, sid, now=NINE, time_limit_seconds=10)
    record_plan(conn, sid, anchor, items, NINE)
    later = NINE + timedelta(days=3)
    due_titles = [c.title for c in due_checkins(conn, sid, later)]
    answers = ["y" if t == "Alpha" else "n" for t in due_titles]
    ask, shown = scripted(answers)
    assert run_checkin(conn, sid, later, ask, shown.append) == 2
    saved = dict(EXTRACTED_TASKS.get(conn, sid))
    assert saved[a].completed_at and not saved[b].completed_at
    assert run_checkin(conn, sid, later, lambda _p: "n", lambda _l: None) == 0  # 'not yet' is not asked again


def test_enter_on_the_check_in_means_not_finished(conn, sid):
    tid = EXTRACTED_TASKS.add(conn, sid, _task("Alpha"))
    anchor, _, items, _ = plan_from_saved(conn, sid, now=NINE, time_limit_seconds=10)
    record_plan(conn, sid, anchor, items, NINE)
    run_checkin(conn, sid, NINE + timedelta(days=3), lambda _p: "", lambda _l: None)
    assert not dict(EXTRACTED_TASKS.get(conn, sid))[tid].completed_at


# ---------- the task menu ----------
def test_menu_asks_the_check_in_as_it_opens(conn, sid):
    tid = EXTRACTED_TASKS.add(conn, sid, _task("Alpha"))
    anchor, _, items, _ = plan_from_saved(conn, sid, now=NINE, time_limit_seconds=10)
    record_plan(conn, sid, anchor, items, NINE)
    ask, shown = scripted(["y", "q"])  # 'yes, finished' to the check-in, then quit
    run_menu(conn, sid, ask, shown.append, now=NINE + timedelta(days=3))
    assert any("Did you finish 'Alpha'?" in l or "ended" in l for l in shown)
    assert dict(EXTRACTED_TASKS.get(conn, sid))[tid].completed_at


def test_menu_check_in_option_says_when_there_is_nothing(conn, sid):
    ask, shown = scripted(["c", "q"])
    run_menu(conn, sid, ask, shown.append, today=D)
    assert "Nothing to check in on right now." in shown


def test_menu_finished_marks_done_and_hides_the_task(conn, sid):
    EXTRACTED_TASKS.add(conn, sid, _task("Essay"))
    ask, shown = scripted(["f", "1", "", "l", "q"])  # Enter = done
    run_menu(conn, sid, ask, shown.append, today=D)
    assert "Marked 'Essay' as done." in shown
    assert shown.count("No tasks saved.") == 1  # the later [l]ist shows only open tasks


def test_menu_can_close_a_task_as_missed_and_keeps_it_as_history(conn, sid):
    tid = EXTRACTED_TASKS.add(conn, sid, _task("Essay"))
    ask, shown = scripted(["f", "1", "x", "m", "q"])  # a bad answer is re-asked
    run_menu(conn, sid, ask, shown.append, today=D)
    saved = dict(EXTRACTED_TASKS.get(conn, sid))[tid]
    assert saved.completed_at and saved.missed  # kept, not deleted
    assert "Type d or m." in shown and "Marked 'Essay' as missed." in shown


def test_finish_task_done_is_not_missed(conn, sid):
    tid = EXTRACTED_TASKS.add(conn, sid, _task("Essay"))
    finish_task(conn, sid, tid, NINE, ask=lambda _p: "", show=lambda _l: None)
    assert dict(EXTRACTED_TASKS.get(conn, sid))[tid].missed is False


def test_closing_an_overdue_task_as_missed_clears_the_overdue_warning(conn, sid):
    tid = EXTRACTED_TASKS.add(conn, sid, _task("Old", due=D - timedelta(days=1)))
    EXTRACTED_TASKS.add(conn, sid, _task("Next"))
    *_, warnings = plan_from_saved(conn, sid, now=NINE, time_limit_seconds=10)
    [w] = [w for w in warnings if w.kind == "task_overdue"]
    assert "done or missed" in w.message and "[f]" in w.message
    finish_task(conn, sid, tid, NINE, ask=lambda _p: "n", show=lambda _l: None, missed=True)
    *_, warnings = plan_from_saved(conn, sid, now=NINE, time_limit_seconds=10)
    assert not [w for w in warnings if w.kind == "task_overdue"]


def test_menu_finished_handles_empty_cancel_and_bad_numbers(conn, sid):
    ask, shown = scripted(["f", "q"])
    run_menu(conn, sid, ask, shown.append, today=D)
    assert "No tasks saved." in shown
    EXTRACTED_TASKS.add(conn, sid, _task("Essay"))
    ask, shown = scripted(["f", "", "f", "9", "q"])
    run_menu(conn, sid, ask, shown.append, today=D)
    assert any("between 1 and 1" in l for l in shown)
    assert not dict(EXTRACTED_TASKS.get(conn, sid))[1].completed_at


def test_menu_reminder_settings_custom_all_off_and_bad_input(conn, sid):
    ask, shown = scripted(["r", "c", "9", "4", "", "q"])  # bad level re-asked, difficulty 4, ignore priority
    run_menu(conn, sid, ask, shown.append, today=D)
    s = load_settings(conn, sid)
    assert (s.reminder_min_difficulty, s.reminder_min_priority, s.reminders_enabled) == (4, None, True)
    assert any("from 1 to 5" in l for l in shown)
    ask, shown = scripted(["r", "o", "r", "a", "r", "zzz", "r", "", "q"])
    run_menu(conn, sid, ask, shown.append, today=D)
    s = load_settings(conn, sid)
    assert s.reminders_enabled and s.reminder_min_difficulty is None and s.reminder_min_priority is None
    assert "Choose a, c or o." in shown

def test_menu_reads_the_time_again_for_each_action(conn, sid):
    tid = EXTRACTED_TASKS.add(conn, sid, _task("Essay"))
    times = iter([NINE, NINE + timedelta(hours=6), NINE + timedelta(hours=6)])  # opening, [f], [q]
    ask, shown = scripted(["f", "1", "d", "q"])
    run_menu(conn, sid, ask, shown.append, clock=lambda: next(times))
    done = dict(EXTRACTED_TASKS.get(conn, sid))[tid].completed_at
    assert done == (NINE + timedelta(hours=6)).isoformat(timespec="minutes")  # not the time the menu opened


def test_finishing_a_task_is_all_or_nothing(conn, sid, monkeypatch):
    tid = EXTRACTED_TASKS.add(conn, sid, _task("Essay", hours=4))
    add_plan_cut(conn, sid, tid, 8)
    def broken(*_a):
        raise RuntimeError("disk full")
    monkeypatch.setattr("scheduler.completion.clear_task_sessions", broken)
    with pytest.raises(RuntimeError):
        finish_task(conn, sid, tid, NINE, ask=lambda _p: "", show=lambda _l: None)
    assert dict(EXTRACTED_TASKS.get(conn, sid))[tid].completed_at is None  # not half-closed
    assert get_plan_cuts(conn, sid) == {tid: 8}


def test_replanning_mid_session_keeps_the_session_in_progress(conn, sid):
    from scheduler.db import due_sessions, record_plan_sessions
    tid = EXTRACTED_TASKS.add(conn, sid, _task("Essay"))
    in_progress, later = (tid, "2026-10-05T08:30", "2026-10-05T09:30"), (tid, "2026-10-05T14:00", "2026-10-05T15:00")
    record_plan_sessions(conn, sid, "2026-10-05T08:00", [in_progress, later])
    record_plan_sessions(conn, sid, "2026-10-05T09:00", [])  # re-plan at 09:00, mid-session
    assert due_sessions(conn, sid, "2026-10-06T00:00") == [in_progress]  # still checked in on; 14:00 replaced


def test_sessions_passed_while_reminders_are_off_do_not_pile_up(conn, sid):  # #16
    tid = EXTRACTED_TASKS.add(conn, sid, _task("Alpha"))
    anchor, _, items, _ = plan_from_saved(conn, sid, now=NINE, time_limit_seconds=10)
    record_plan(conn, sid, anchor, items, NINE)
    _set(conn, sid, reminders_enabled=False)
    later = NINE + timedelta(days=3)
    assert run_checkin(conn, sid, later, lambda _p: "n", lambda _l: None) == 0
    _set(conn, sid, reminders_enabled=True)  # turned back on: the old sessions were already let go
    assert due_checkins(conn, sid, later) == []
    assert not dict(EXTRACTED_TASKS.get(conn, sid))[tid].completed_at
