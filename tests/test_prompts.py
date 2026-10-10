import pytest

from scheduler.models import ExtractedTask
from scheduler.prompts import (ask_missed, ask_until, confirm, describe_task, parse_due, parse_hours,
                               parse_rating, parse_whole, pick)


def scripted(answers):
    it, shown = iter(answers), []
    return (lambda _: next(it)), shown


def test_ask_until_reprompts_on_invalid_then_parses():
    answers, shown = iter(["x", "7"]), []
    assert ask_until(lambda _: next(answers), shown.append, "N", int) == 7
    assert len(shown) == 1 and shown[0].startswith("  Invalid:")


def test_ask_until_enter_returns_default():
    assert ask_until(lambda _: "", print, "N", int) is None
    assert ask_until(lambda _: "", print, "N", int, default=3) == 3


def test_ask_until_required_reasks_on_enter():
    ask, shown = scripted(["", "Essay"])
    assert ask_until(ask, shown.append, "title", str, required=True) == "Essay"
    assert shown == ["  title is required."]


def test_pick_by_number_and_rejects_out_of_range():
    shown = []
    assert pick(lambda _: "2", shown.append, ["a", "b"], "> ") == "b"
    assert pick(lambda _: "3", shown.append, ["a", "b"], "> ") is None
    assert pick(lambda _: "", shown.append, ["a", "b"], "> ") is None
    assert shown == ["Enter a number between 1 and 2."]  # Enter cancels quietly


def test_confirm_enter_takes_default_and_junk_reasks():
    ask, _ = scripted(["", "maybe", "YES"])
    assert confirm(ask, "Sure?", True) is True
    assert confirm(ask, "Sure?", False) is True  # "maybe" re-asked, then YES


def test_ask_missed_enter_is_done_and_junk_reasks():
    ask, shown = scripted(["", "x", "m"])
    assert ask_missed(ask, shown.append, "'Essay':") is False
    assert ask_missed(ask, shown.append, "'Essay':") is True
    assert shown == ["Type d or m."]


@pytest.mark.parametrize("text, lo, hi, n", [("1", 1, 5, 1), ("5", 1, 5, 5), (" 3 ", 1, 5, 3), ("0", 0, 9, 0)])
def test_parse_whole_accepts_the_range(text, lo, hi, n):
    assert parse_whole(text, lo, hi) == n


@pytest.mark.parametrize("text", ["0", "6", "2.5", "abc", "-1"])
def test_parse_whole_rejects_outside_the_range(text):
    with pytest.raises(ValueError, match="from 1 to 5"):
        parse_whole(text, 1, 5)


def test_parse_rating_is_one_to_five():
    assert parse_rating("4") == 4
    with pytest.raises(ValueError, match="from 1 to 5"):
        parse_rating("9")


@pytest.mark.parametrize("text, slots", [("1", 4), ("1.5", 6), ("0.25", 1), ("2.1", 8)])
def test_parse_hours_returns_slots(text, slots):
    assert parse_hours(text) == slots


@pytest.mark.parametrize("text", ["abc", "", "0", "0.1", "-2"])
def test_parse_hours_rejects_junk_and_under_one_slot(text):
    with pytest.raises(ValueError):
        parse_hours(text)


def test_parse_due_date_alone_or_with_a_rounded_time():
    assert parse_due("2026-10-02") == ("2026-10-02", None)
    assert parse_due("2026-10-02 9:10") == ("2026-10-02", "09:00")
    assert parse_due("2026-10-02 24:00") == ("2026-10-02", "24:00")
    with pytest.raises(ValueError):
        parse_due("2026-02-30")


def test_describe_task_notes_one_block_split_and_sleep_leave():
    t = ExtractedTask(title="Essay", date="2026-10-05", duration_slots=12, splittable=False)
    assert describe_task(t) == "Essay due 2026-10-05 (3h, priority 3, difficulty 3, one block)"
    t = t.model_copy(update={"splittable": True, "may_cut_sleep": True})
    assert describe_task(t, session_cap=8) == ("Essay due 2026-10-05 (3h, priority 3, difficulty 3, "
                                               "can be split, may use sleep below target)")
    assert describe_task(t, session_cap=None).endswith("difficulty 3, may use sleep below target)")
