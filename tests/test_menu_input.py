from scheduler.menu_input import ask_until, pick


def test_ask_until_reprompts_on_invalid_then_parses():
    answers, shown = iter(["x", "7"]), []
    assert ask_until(lambda _: next(answers), shown.append, "N", int) == 7
    assert len(shown) == 1 and shown[0].startswith("  Invalid:")


def test_ask_until_enter_returns_default():
    assert ask_until(lambda _: "", print, "N", int) is None
    assert ask_until(lambda _: "", print, "N", int, default=3) == 3


def test_pick_by_number_and_rejects_out_of_range():
    shown = []
    assert pick(lambda _: "2", shown.append, ["a", "b"], "> ") == "b"
    assert pick(lambda _: "3", shown.append, ["a", "b"], "> ") is None
    assert pick(lambda _: "", shown.append, ["a", "b"], "> ") is None
    assert shown == ["Enter a number between 1 and 2."]  # Enter cancels quietly
