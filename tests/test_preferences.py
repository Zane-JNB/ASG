import pytest

from scheduler.db import (
    connect, get_or_create_student, get_reflections, load_evidence, load_settings, load_tiers,
    save_evidence,
)
from scheduler.preference_policy import Tier
from scheduler.preferences import Actor, PreferenceError, change_tier, set_values, user_edit


@pytest.fixture
def conn():
    return connect(":memory:")


@pytest.fixture
def sid(conn):
    return get_or_create_student(conn, "Zane")


def tier(conn, sid, field):
    return load_tiers(conn, sid)[field]


def test_locked_field_rejects_user_edit_and_tier_change(conn, sid):
    before = load_settings(conn, sid)
    with pytest.raises(PreferenceError, match="protected"):
        user_edit(conn, sid, "presence_bonus", 5)
    with pytest.raises(PreferenceError):
        change_tier(conn, sid, "presence_bonus", Tier.USER, Actor.USER)   # can't unlock/claim
    assert load_settings(conn, sid) == before and tier(conn, sid, "presence_bonus") == Tier.LOCKED


@pytest.mark.parametrize("field", ["bedtime_penalty", "same_day_penalty", "sleep_target_penalty"])
def test_internal_weights_cannot_be_edited_or_claimed(conn, sid, field):
    assert tier(conn, sid, field) == Tier.MODEL_LEARNED          # model-learned, yet hidden from users
    with pytest.raises(PreferenceError, match="internal"):
        user_edit(conn, sid, field, 1)
    with pytest.raises(PreferenceError):
        change_tier(conn, sid, field, Tier.USER, Actor.USER)
    assert tier(conn, sid, field) == Tier.MODEL_LEARNED


def test_user_cannot_edit_model_owned_field_until_claimed(conn, sid):
    with pytest.raises(PreferenceError, match="Take ownership"):
        user_edit(conn, sid, "buffer_slots", 3)
    assert change_tier(conn, sid, "buffer_slots", Tier.USER, Actor.USER) is True
    assert user_edit(conn, sid, "buffer_slots", 3).buffer_slots == 3


def test_claim_keeps_value_and_clears_only_that_fields_evidence(conn, sid):
    set_values(conn, sid, {"buffer_slots": 3}, Actor.INTERNAL)
    save_evidence(conn, sid, "buffer_slots", 2, "small")
    save_evidence(conn, sid, "bedtime_penalty", 2, "small")
    change_tier(conn, sid, "buffer_slots", Tier.USER, Actor.USER)
    assert load_settings(conn, sid).buffer_slots == 3
    assert set(load_evidence(conn, sid)) == {"bedtime_penalty"}


def test_return_to_model_keeps_value_and_starts_clean(conn, sid):
    # default_max_session_slots starts as user-owned; handing it to the model is the reverse path
    user_edit(conn, sid, "default_max_session_slots", 6)
    save_evidence(conn, sid, "default_max_session_slots", 1, "small")   # stale leftover
    assert change_tier(conn, sid, "default_max_session_slots", Tier.MODEL_LEARNED, Actor.USER)
    assert load_settings(conn, sid).default_max_session_slots == 6
    assert "default_max_session_slots" not in load_evidence(conn, sid)
    with pytest.raises(PreferenceError):
        user_edit(conn, sid, "default_max_session_slots", 4)            # now model-owned again


def test_same_tier_change_is_a_noop_and_keeps_evidence(conn, sid):
    save_evidence(conn, sid, "buffer_slots", 2, "small")
    assert change_tier(conn, sid, "buffer_slots", Tier.MODEL_LEARNED, Actor.USER) is False
    assert "buffer_slots" in load_evidence(conn, sid)


def test_user_only_fields_editable_but_not_claimable(conn, sid):
    assert user_edit(conn, sid, "default_sleep_min_slots", 20).default_sleep_min_slots == 20
    assert user_edit(conn, sid, "reminders_enabled", False).reminders_enabled is False
    with pytest.raises(PreferenceError):
        change_tier(conn, sid, "default_sleep_min_slots", Tier.MODEL_LEARNED, Actor.USER)


def test_edit_is_validated_and_failed_edits_change_nothing(conn, sid):
    before, n_log = load_settings(conn, sid), len(get_reflections(conn, sid))
    for field, bad in [("default_max_session_slots", -3), ("default_sleep_min_slots", 40),   # > target 32
                       ("default_earliest_bed", 96)]:                                        # > preferred 92
        with pytest.raises(PreferenceError, match="Invalid"):
            user_edit(conn, sid, field, bad)
    assert load_settings(conn, sid) == before and len(get_reflections(conn, sid)) == n_log


def test_multi_field_edit_is_atomic_and_allows_moving_the_bed_window(conn, sid):
    change_tier(conn, sid, "default_preferred_bed", Tier.USER, Actor.USER)   # preferred bed starts model-owned
    new = set_values(conn, sid, {"default_earliest_bed": 96, "default_preferred_bed": 100,
                                 "default_latest_bed": 108}, Actor.USER)
    assert (new.default_earliest_bed, new.default_latest_bed) == (96, 108)
    before = load_settings(conn, sid)
    with pytest.raises(PreferenceError):   # session ok, buffer model-owned -> whole edit refused
        set_values(conn, sid, {"default_max_session_slots": 6, "buffer_slots": 3}, Actor.USER)
    assert load_settings(conn, sid) == before


def test_model_actor_cannot_edit_or_change_tiers(conn, sid):
    with pytest.raises(PreferenceError):
        set_values(conn, sid, {"default_max_session_slots": 6}, Actor.MODEL)   # even a user-owned field
    with pytest.raises(PreferenceError):
        change_tier(conn, sid, "buffer_slots", Tier.USER, Actor.MODEL)


def test_unknown_field_is_rejected(conn, sid):
    with pytest.raises(PreferenceError, match="not a known setting"):
        user_edit(conn, sid, "made_up", 1)


def test_internal_actor_may_edit_locked_fields(conn, sid):
    assert set_values(conn, sid, {"presence_bonus": 20_000}, Actor.INTERNAL).presence_bonus == 20_000


def test_edits_and_tier_changes_are_audited_and_noops_are_not(conn, sid):
    user_edit(conn, sid, "default_max_session_slots", 6)
    user_edit(conn, sid, "default_max_session_slots", 6)                  # no-op: no new row
    change_tier(conn, sid, "buffer_slots", Tier.USER, Actor.USER)
    log = get_reflections(conn, sid)
    assert [r["outcome"] for r in log] == ["user_edit", "ownership_change"]
    assert log[0]["settings_before"].default_max_session_slots == 8 and log[0]["applied"] is True
    assert log[1]["applied"] is False


def test_students_are_isolated(conn, sid):
    other = get_or_create_student(conn, "Other")
    change_tier(conn, sid, "buffer_slots", Tier.USER, Actor.USER)
    user_edit(conn, sid, "buffer_slots", 4)
    assert tier(conn, other, "buffer_slots") == Tier.MODEL_LEARNED
    assert load_settings(conn, other).buffer_slots == 1

def test_live_evidence_accepts_naive_and_aware_now(conn, sid):
    from datetime import datetime, timezone
    from scheduler.db import save_evidence
    from scheduler.preferences import live_evidence
    save_evidence(conn, sid, "buffer_slots", 1, 1, at=datetime(2026, 9, 1, tzinfo=timezone.utc).isoformat())
    assert live_evidence(conn, sid, datetime(2026, 9, 2)) == live_evidence(
        conn, sid, datetime(2026, 9, 2, tzinfo=timezone.utc)) != {}
