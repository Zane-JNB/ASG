from types import SimpleNamespace

from scheduler.manual_cuts import NEW, CutState
from scheduler.manual_review import ask_mode, run_manual_edit


def even_chunks(i, remaining, cap=8):  # stand-in for chunk_sizes()
    n = -(-remaining // cap)
    base, extra = divmod(remaining, n)
    return [base + 1] * extra + [base] * (n - extra)


TITLES = {0: "Lab", 1: "Quiz", NEW: "Essay"}  # screen order: Lab=1, Quiz=2, Essay (new)=3


def state():
    return CutState({0: 20, 1: 6, NEW: 8}, even_chunks)


def fits_after_freeing(n, flags=(), sleep=0):
    """Fake solver: everything fits once at least n slots have been cut in total."""
    return lambda lost: (SimpleNamespace(flags=list(flags), sleep_sacrificed_slots=sleep)
                         if sum(lost.values()) >= n else None)


def run(answers, fits, must_add=False, st=None):
    it, shown = iter(answers), []
    out = run_manual_edit(st or state(), TITLES, fits, ask=lambda p: (shown.append(p), next(it))[1],
                          show=shown.append, must_add=must_add)  # prompts are recorded in `shown` too
    return out, shown


def test_cut_until_it_fits_then_save():
    (action, p), shown = run(["2", "d", "s"], fits_after_freeing(6))  # drop Quiz (6 slots)
    assert action == "save" and p is not None
    assert "Everything fits now." in shown


def test_save_is_refused_until_it_fits_and_the_student_can_keep_cutting():
    # reduce Quiz by 1h (4 slots): not enough -> "s" refused -> cut 1 session of Lab (6 more) -> fits -> save
    (action, p), shown = run(["2", "t", "1", "s", "1", "s", "1", "s"], fits_after_freeing(10))
    assert action == "save" and p is not None
    assert "Still doesn't fit -- keep cutting, undo, or cancel." in shown
    assert "It doesn't fit yet, so there is nothing to save." in shown


def test_not_fitting_after_a_cut_says_so_and_does_not_end():
    (action, _), shown = run(["2", "d", "1", "t", "1", "", "y"], fits_after_freeing(99))
    assert action == "cancel"          # only ended because the student chose to
    assert shown.count("Still doesn't fit -- keep cutting, undo, or cancel.") >= 2


def test_dont_add_discards_everything_and_returns_no_proposal():
    (action, p), _ = run(["2", "d", "n"], fits_after_freeing(99))
    assert (action, p) == ("dont_add", None)


def test_dont_add_is_not_offered_when_the_task_must_be_added():
    (action, _), shown = run(["n", ""], fits_after_freeing(1), must_add=True)
    assert action == "cancel"
    assert not any("don't add" in l for l in shown)
    assert any(l.startswith("Enter a number from 1 to 3") for l in shown)  # "n" is just invalid here


def test_enter_cancels_at_once_when_nothing_was_cut():
    (action, p), _ = run([""], fits_after_freeing(1))
    assert (action, p) == ("cancel", None)


def test_cancel_after_cutting_asks_first_and_n_keeps_editing():
    (action, _), shown = run(["2", "d", "", "n", "", "y"], fits_after_freeing(99))
    assert action == "cancel"
    assert sum(l.startswith("Discard your cuts") for l in shown) == 2  # "n" kept editing, "y" cancelled


def test_the_new_task_can_be_shortened_but_not_dropped():
    st = state()
    # pick Essay (3), try "d" (refused, back to the list), pick it again, reduce by 1h, save
    (action, _), shown = run(["3", "d", "3", "t", "1", "s"], fits_after_freeing(4), st=st)
    assert action == "save" and st.lost == {NEW: 4}
    assert "  [t]ime  (Enter to go back): " in shown      # no [d]rop offered for the new task
    assert "  Choose one of the options shown." in shown


def test_undo_restores_the_list_and_forgets_the_fit():
    st = state()
    (action, _), shown = run(["2", "d", "u", ""], fits_after_freeing(6), st=st)
    assert action == "cancel" and st.lost == {} and "Undid the last edit." in shown


def test_bad_input_gets_a_message_and_changes_nothing():
    st = state()
    (action, _), shown = run(["9", "x", "2", "t", "abc", "2", "s", "", ""], fits_after_freeing(1), st=st)
    assert st.lost == {} and action == "cancel"
    assert any("Not done" in l for l in shown) and any("Enter a number" in l for l in shown)


def test_sessions_option_only_for_split_tasks_and_never_all_of_them():
    st = state()  # Lab = 3 sessions, Quiz = 1
    # Quiz (no sessions option) -> back; Lab -> sessions -> "3" rejected -> again -> "1" cuts the last session
    (action, _), shown = run(["2", "", "1", "s", "3", "1", "s", "1", "s"], fits_after_freeing(1), st=st)
    edit_prompts = [l for l in shown if l.startswith("  [")]
    assert "[s]essions" not in edit_prompts[0] and "[s]essions" in edit_prompts[1]
    assert "  Cut how many of its 3 sessions (1-2)? " in shown   # never all 3: that is "drop"
    assert "  Not done: enter a whole number from 1 to 2" in shown
    assert action == "save" and st.lost == {0: 6}


def test_flags_and_sleep_shortfall_are_shown_before_saving():
    (action, _), shown = run(["2", "d", "s"], fits_after_freeing(
        6, flags=["'Lab' would fall short of its deadline"], sleep=8))
    assert any("Sleep: 2h below target" in l for l in shown)
    assert any("fall short of its deadline" in l for l in shown)


def test_ask_mode_accepts_m_and_s_and_blocks_automatic():
    it, shown = iter(["a", "z", "M"]), []
    assert ask_mode(ask=lambda _p: next(it), show=shown.append) == "m"
    assert any("isn't available yet" in l for l in shown) and any("Choose m, s or a" in l for l in shown)
    assert ask_mode(ask=lambda _p: "") is None