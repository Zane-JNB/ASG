import pytest

from scheduler.db import connect, get_or_create_student, load_settings, get_reflections, save_settings
from scheduler.reflection import ReflectionResult, PreferenceChangeProposal
from scheduler.reflection_cycle import get_proposals, apply_and_log, rerun_schedule
from scheduler.models import DynamicTask, ProfileSettings


@pytest.fixture
def conn():
    return connect(":memory:")


@pytest.fixture
def student_id(conn):
    return get_or_create_student(conn, "Zane")


def make_result(*proposals) -> ReflectionResult:
    return ReflectionResult(summary="test summary", proposals=list(proposals))


def test_get_proposals_uses_fake_backend_by_default(monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    result = get_proposals("it felt rushed, no breaks")
    assert isinstance(result, ReflectionResult)
    assert any(p.field == "buffer_slots" for p in result.proposals)


def test_apply_and_log_applies_only_accepted_proposals(conn, student_id):
    result = make_result(
        PreferenceChangeProposal(field="buffer_slots", direction="increase",
                                 magnitude="small", reason="a"),
        PreferenceChangeProposal(field="same_day_penalty", direction="decrease",
                                 magnitude="medium", reason="b"),
    )
    before = load_settings(conn, student_id)
    after = apply_and_log(conn, student_id, "some reflection", result, accepted=[True, False])

    assert after.buffer_slots == before.buffer_slots + 1
    assert after.same_day_penalty == before.same_day_penalty  # rejected -- untouched


def test_apply_and_log_saves_to_db(conn, student_id):
    result = make_result(
        PreferenceChangeProposal(field="buffer_slots", direction="increase",
                                 magnitude="large", reason="a"),
    )
    apply_and_log(conn, student_id, "reflection", result, accepted=[True])
    reloaded = load_settings(conn, student_id)
    assert reloaded.buffer_slots == 1 + 4  # default 1 + large delta of 4


def test_apply_and_log_logs_reflection_with_applied_true(conn, student_id):
    result = make_result(
        PreferenceChangeProposal(field="buffer_slots", direction="increase",
                                 magnitude="small", reason="a"),
    )
    apply_and_log(conn, student_id, "felt rushed", result, accepted=[True])
    history = get_reflections(conn, student_id)
    assert len(history) == 1
    assert history[0]["applied"] is True
    assert history[0]["reflection_text"] == "felt rushed"


def test_apply_and_log_with_nothing_accepted_does_not_save_but_still_logs(conn, student_id):
    result = make_result(
        PreferenceChangeProposal(field="buffer_slots", direction="increase",
                                 magnitude="small", reason="a"),
    )
    before = load_settings(conn, student_id)
    after = apply_and_log(conn, student_id, "meh", result, accepted=[False])

    assert after == before
    history = get_reflections(conn, student_id)
    assert len(history) == 1
    assert history[0]["applied"] is False


def test_apply_and_log_with_zero_proposals(conn, student_id):
    result = make_result()
    after = apply_and_log(conn, student_id, "all fine", result, accepted=[])
    before = load_settings(conn, student_id)
    assert after == before
    assert get_reflections(conn, student_id)[0]["applied"] is False


def test_apply_and_log_rejects_mismatched_accepted_length(conn, student_id):
    result = make_result(
        PreferenceChangeProposal(field="buffer_slots", direction="increase",
                                 magnitude="small", reason="a"),
    )
    with pytest.raises(ValueError, match="accepted has"):
        apply_and_log(conn, student_id, "x", result, accepted=[True, True])

def test_rerun_schedule_uses_this_students_current_settings(conn, student_id):
    save_settings(conn, student_id, ProfileSettings(buffer_slots=5))
    tasks = [
        DynamicTask(title="A", duration_slots=4, priority=3, difficulty=2, splittable=False),
        DynamicTask(title="B", duration_slots=4, priority=3, difficulty=2, splittable=False),
    ]
    items, unscheduled = rerun_schedule(conn, student_id, fixed_blocks=[], tasks=tasks, num_days=1)
    assert unscheduled == []
    task_items = sorted((i for i in items if i.kind == "task"), key=lambda i: i.start_slot)
    gap = task_items[1].start_slot - task_items[0].end_slot
    assert gap >= 5


def test_rerun_schedule_picks_up_a_settings_change_made_between_calls(conn, student_id):
    tasks = [
        DynamicTask(title="A", duration_slots=4, priority=3, difficulty=2, splittable=False),
        DynamicTask(title="B", duration_slots=4, priority=3, difficulty=2, splittable=False),
    ]
    items_before, _ = rerun_schedule(conn, student_id, fixed_blocks=[], tasks=tasks, num_days=1)
    gap_before = _gap_between(items_before)
    save_settings(conn, student_id, ProfileSettings(buffer_slots=6))
    items_after, _ = rerun_schedule(conn, student_id, fixed_blocks=[], tasks=tasks, num_days=1)
    gap_after = _gap_between(items_after)
    assert gap_after > gap_before


def _gap_between(items):
    task_items = sorted((i for i in items if i.kind == "task"), key=lambda i: i.start_slot)
    return task_items[1].start_slot - task_items[0].end_slot