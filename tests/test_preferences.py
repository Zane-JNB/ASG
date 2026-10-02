
import pytest
from scheduler.db import load_evidence, load_evidence, load_settings, load_settings, save_evidence
from scheduler.preference_policy import Tier    , user_edit
from scheduler.preferences import Actor, PreferenceError, change_tier, set_values, user_edit, set_values, change_tier, user_edit


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

def test_model_actor_cannot_edit_or_change_tiers(conn, sid):
    with pytest.raises(PreferenceError):
        set_values(conn, sid, {"default_max_session_slots": 6}, Actor.MODEL)
    with pytest.raises(PreferenceError):
        change_tier(conn, sid, "buffer_slots", Tier.USER, Actor.MODEL)