import pytest
from scheduler.manual_cuts import NEW, CutState


def even_chunks(i, remaining, cap=8):  # stand-in for chunk_sizes()
    n = -(-remaining // cap)
    base, extra = divmod(remaining, n)
    return [base + 1] * extra + [base] * (n - extra)


def state():
    return CutState({0: 20, 1: 6, NEW: 8}, even_chunks)  # task 0 = 3 chunks (7,7,6)


def test_drop_takes_everything_left():
    s = state()
    assert s.drop(1) == 6 and s.remaining(1) == 0


def test_cut_chunks_removes_trailing_sessions():
    s = state()
    assert s.cut_chunks(0, 1) == 6 and s.remaining(0) == 14


def test_chunk_cuts_work_on_what_is_left_after_a_reduce():
    s = state()
    s.reduce(0, 6)                      # 20 -> 14, now 2 chunks of 7
    assert s.cut_chunks(0, 1) == 7 and s.remaining(0) == 7


def test_reduce_must_leave_something():
    s = state()
    with pytest.raises(ValueError):
        s.reduce(1, 6)
    with pytest.raises(ValueError):
        s.reduce(1, 0)


def test_cannot_cut_more_chunks_than_exist():
    with pytest.raises(ValueError):
        state().cut_chunks(0, 4)


def test_fully_cut_task_cannot_be_edited_again():
    s = state()
    s.drop(1)
    with pytest.raises(ValueError):
        s.reduce(1, 1)


def test_new_task_is_editable_like_any_other():
    s = state()
    s.reduce(NEW, 3)
    assert s.remaining(NEW) == 5 and s.lost == {NEW: 3}


def test_undo_steps_back_one_edit_at_a_time():
    s = state()
    s.drop(1); s.reduce(0, 2)
    assert s.undo() and s.lost == {1: 6}
    assert s.undo() and s.lost == {} and not s.touched
    assert s.undo() is False


def test_failed_edit_changes_nothing():
    s = state()
    with pytest.raises(ValueError):
        s.cut_chunks(0, 9)
    assert s.lost == {} and s._undo == []