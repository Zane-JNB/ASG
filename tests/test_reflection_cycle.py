from types import SimpleNamespace

import pytest

from scheduler.db import connect, get_or_create_student, get_reflections, load_settings
from scheduler.reflection import PreferenceChangeProposal, ReflectionResult, propose_preference_changes
from scheduler.reflection_cycle import apply_and_log
from scheduler.preferences import Actor, change_tier
from scheduler.preference_policy import Tier
from scheduler.reflection import build_system_prompt
from scheduler.reflection_cycle import reflect_and_record

@pytest.fixture
def conn():
    return connect(":memory:")


@pytest.fixture
def student_id(conn):
    return get_or_create_student(conn, "Zane")


def make_result(*proposals) -> ReflectionResult:
    return ReflectionResult(summary="test summary", proposals=list(proposals))


def test_propose_preference_changes_goes_through_the_provider_table():  # conftest's offline stub answers
    result = propose_preference_changes("it felt rushed, no breaks")
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
    assert history[0].applied is True
    assert history[0].reflection_text == "felt rushed"


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
    assert history[0].applied is False


def test_apply_and_log_with_zero_proposals(conn, student_id):
    result = make_result()
    after = apply_and_log(conn, student_id, "all fine", result, accepted=[])
    before = load_settings(conn, student_id)
    assert after == before
    assert get_reflections(conn, student_id)[0].applied is False


def test_apply_and_log_rejects_mismatched_accepted_length(conn, student_id):
    result = make_result(
        PreferenceChangeProposal(field="buffer_slots", direction="increase",
                                 magnitude="small", reason="a"),
    )
    with pytest.raises(ValueError, match="accepted has"):
        apply_and_log(conn, student_id, "x", result, accepted=[True, True])



def _gap_between(items):
    task_items = sorted((i for i in items if i.kind == "task"), key=lambda i: i.start_slot)
    return task_items[1].start_slot - task_items[0].end_slot

def test_prompt_lists_only_allowed_fields():
    p = build_system_prompt(["buffer_slots"])
    assert "buffer_slots (deltas" in p and "bedtime_penalty (deltas" not in p

def test_user_owned_field_is_not_shown_to_the_model():
    conn = connect(":memory:"); sid = get_or_create_student(conn, "Zane")
    change_tier(conn, sid, "buffer_slots", Tier.USER, Actor.USER)
    seen = {}
    def create(**kw):
        seen["system"] = kw["messages"][0]["content"]  # OpenAI shape: system prompt is message 0
        call = SimpleNamespace(function=SimpleNamespace(arguments='{"summary": "", "proposals": []}'))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call]))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    reflect_and_record(conn, sid, "felt rushed", client=client)
    assert "buffer_slots (deltas" not in seen["system"] and "bedtime_penalty (deltas" in seen["system"]

def test_apply_and_log_skips_fields_the_student_owns(conn, student_id):
    change_tier(conn, student_id, "buffer_slots", Tier.USER, Actor.USER)  # student claims it
    result = make_result(PreferenceChangeProposal(field="buffer_slots", direction="increase",
                                                  magnitude="small", reason="a"))
    before = load_settings(conn, student_id)
    after = apply_and_log(conn, student_id, "rushed", result, accepted=[True])
    assert after == before


def test_apply_and_log_skips_a_proposal_that_would_make_settings_invalid(conn, student_id, monkeypatch):
    import scheduler.reflection_cycle as rc
    bad = make_result(PreferenceChangeProposal(field="buffer_slots", direction="increase",
                                               magnitude="small", reason="a"))
    def to_invalid(settings, field, direction, magnitude):  # pretend the bucket produced an out-of-range value
        return -5
    monkeypatch.setattr(rc, "stepped_value", to_invalid)
    before = load_settings(conn, student_id)
    assert apply_and_log(conn, student_id, "x", bad, accepted=[True]) == before


def test_apply_and_log_is_all_or_nothing(conn, student_id, monkeypatch):
    before = load_settings(conn, student_id)
    def broken(*_a, **_kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr("scheduler.reflection_cycle.log_reflection", broken)
    result = make_result(PreferenceChangeProposal(field="buffer_slots", direction="increase",
                                                  magnitude="small", reason="rushed"))
    with pytest.raises(RuntimeError):
        apply_and_log(conn, student_id, "rushed", result, [True])
    assert load_settings(conn, student_id) == before  # no settings change without its log row
