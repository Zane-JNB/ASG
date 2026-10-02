from datetime import date

from scheduler.commute_menu import run_commute_menu
from scheduler.commutes import expand_commutes
from scheduler.db import connect, get_commutes, get_or_create_student
from scheduler.models import PlanAnchor

TODAY = date(2026, 10, 5)  # a Monday


def scripted(answers):
    it = iter(answers)
    shown = []
    return (lambda _prompt: next(it)), shown


def _run(answers):
    conn = connect(":memory:")
    sid = get_or_create_student(conn, "Z")
    ask, shown = scripted(answers)
    run_commute_menu(conn, sid, ask, shown.append, today=TODAY)
    return conn, sid, shown


def _saved(conn, sid):
    return [c for _, c in get_commutes(conn, sid)]


def test_recurring_commute_on_several_weekdays_makes_one_each():
    conn, sid, shown = _run(["a", "", "08:00", "45", "y", "Fri, mon wed", "b"])
    saved = _saved(conn, sid)
    assert [(c.weekday, c.start_time, c.length_minutes) for c in saved] == [
        ("Mon", "08:00", 45), ("Wed", "08:00", 45), ("Fri", "08:00", 45)]
    assert all(c.recurring and c.title == "Commute" for c in saved)


def test_one_time_commute_defaults_to_today_and_keeps_its_name():
    conn, sid, _ = _run(["a", "Bus home", "17:30", "30", "", "", "b"])  # Enter = not weekly, Enter = today
    [c] = _saved(conn, sid)
    assert (c.title, c.date, c.recurring, c.length_minutes) == ("Bus home", "2026-10-05", False, 30)


def test_bad_input_reasks_and_a_past_date_is_refused():
    conn, sid, shown = _run(["a", "", "25:00", "7am", "07:45", "abc", "0", "20", "n", "2026-10-04", "2026-10-06", "b"])
    [c] = _saved(conn, sid)
    assert (c.start_time, c.length_minutes, c.date) == ("07:45", 20, "2026-10-06")
    assert sum("Invalid" in s for s in shown) == 5


def test_enter_on_the_start_time_cancels_and_saves_nothing():
    conn, sid, shown = _run(["a", "", "", "b"])
    assert _saved(conn, sid) == [] and "Cancelled -- nothing saved." in shown


def test_two_commutes_on_the_same_day_are_both_kept():
    conn, sid, _ = _run(["a", "To campus", "08:00", "40", "y", "Mon", "a", "Home", "17:00", "40", "y", "Mon", "b"])
    assert [c.title for c in _saved(conn, sid)] == ["To campus", "Home"]


def test_skipping_one_date_keeps_the_commute_but_drops_that_day_from_the_plan():
    conn, sid, shown = _run(["a", "", "08:00", "45", "y", "Mon", "s", "1", "o", "2026-10-12", "b"])
    [c] = _saved(conn, sid)
    assert c.skip_dates == ["2026-10-12"]
    blocks = expand_commutes([c], PlanAnchor(start_date=TODAY, num_days=8))
    assert [b.day for b in blocks] == [0]  # Mon 5 Oct only; Mon 12 Oct is skipped


def test_skipping_a_date_on_the_wrong_weekday_changes_nothing():
    conn, sid, shown = _run(["a", "", "08:00", "45", "y", "Mon", "s", "1", "o", "2026-10-06", "b"])
    assert _saved(conn, sid)[0].skip_dates == []
    assert any("not a Mon" in s for s in shown)


def test_ending_a_recurring_commute_stops_it_after_that_date():
    conn, sid, _ = _run(["a", "", "08:00", "45", "y", "Mon", "s", "1", "e", "2026-10-12", "b"])
    [c] = _saved(conn, sid)
    assert c.end_date == "2026-10-12"
    blocks = expand_commutes([c], PlanAnchor(start_date=TODAY, num_days=15))
    assert [b.day for b in blocks] == [0, 7]  # 5 and 12 Oct run, 19 Oct does not


def test_skipping_a_one_time_commute_deletes_it_only_when_confirmed():
    conn, sid, _ = _run(["a", "", "17:00", "30", "n", "", "s", "1", "n", "s", "1", "y", "b"])
    assert _saved(conn, sid) == []  # first "n" kept it, second "y" deleted it


def test_delete_removes_the_chosen_commute_and_list_is_ordered():
    conn, sid, shown = _run(["a", "", "09:00", "30", "y", "Wed", "a", "", "08:00", "30", "y", "Mon",
                             "l", "d", "1", "l", "b"])
    assert [c.weekday for c in _saved(conn, sid)] == ["Wed"]
    assert shown.index("1. Commute: every Mon 08:00, 30 min") < shown.index("2. Commute: every Wed 09:00, 30 min")