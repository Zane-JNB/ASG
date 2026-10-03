import pytest
from scheduler.models import ProfileSettings
from scheduler.preference_policy import MODEL_DELTAS, POLICY, FieldPolicy, Tier
from scheduler.reflection import ADJUSTABLE_FIELDS

PENALTIES = [n for n in ProfileSettings.model_fields if n.endswith("_penalty")]

def test_every_profile_setting_has_a_policy_entry_and_no_strays():
    fields = set(ProfileSettings.model_fields)
    assert fields - set(POLICY) == set(), "new ProfileSettings field needs a POLICY entry"
    assert set(POLICY) - fields == set()

@pytest.mark.parametrize("name", PENALTIES + ["drop_deadline_multiplier"])
def test_penalties_and_deadline_multiplier_are_never_user_editable(name):
    assert not POLICY[name].user_editable and not POLICY[name].user_claimable

def test_claimable_set_and_default_tiers():
    assert {n for n, p in POLICY.items() if p.user_claimable} == {
        "buffer_slots", "default_max_session_slots", "default_sleep_length_slots", "default_preferred_bed"}
    assert POLICY["default_max_session_slots"].default_tier == Tier.USER

@pytest.mark.parametrize("kwargs", [
    dict(default_tier=Tier.LOCKED, user_editable=True),
    dict(default_tier=Tier.USER),
    dict(default_tier=Tier.MODEL_LEARNED),
])
def test_invalid_policy_combinations_are_rejected(kwargs):
    dict(default_tier=Tier.USER, user_editable=True, user_claimable=True),  # claimable, not learnable
    dict(default_tier=Tier.USER, user_editable=True, deltas={"small": 1}),  # deltas w/o learnable
    with pytest.raises(ValueError):
        FieldPolicy(label="x", description="y", **kwargs)

def test_adjustable_fields_unchanged_by_the_refactor():  # compare to your old dict's 7 entries
    assert ADJUSTABLE_FIELDS == MODEL_DELTAS
    assert set(ADJUSTABLE_FIELDS) == {"buffer_slots", "bedtime_penalty", "same_day_penalty",
        "default_max_session_slots", "sleep_target_penalty", "default_sleep_length_slots", "default_preferred_bed"}

def test_tier_serialized_values_are_exact():
    assert [t.value for t in Tier] == ["locked", "user", "model_learned"]


def test_adjustable_fields_unchanged_by_the_refactor():   # replace the short version
    assert ADJUSTABLE_FIELDS == MODEL_DELTAS == {
        "buffer_slots": {"small": 1, "medium": 2, "large": 4},
        "bedtime_penalty": {"small": 10, "medium": 25, "large": 50},
        "same_day_penalty": {"small": 500, "medium": 1000, "large": 2000},
        "default_max_session_slots": {"small": 2, "medium": 4, "large": 8},
        "sleep_target_penalty": {"small": 500, "medium": 1000, "large": 2500},
        "default_sleep_length_slots": {"small": 2, "medium": 4, "large": 8},
        "default_preferred_bed": {"small": 2, "medium": 4, "large": 8},
    }


def test_locked_fields_are_fully_closed():
    locked = {n for n, p in POLICY.items() if p.default_tier == Tier.LOCKED}
    assert {"presence_bonus", "sleep_min_penalty", "drop_deadline_multiplier"} <= locked
    for n in locked:
        p = POLICY[n]
        assert not (p.model_learnable or p.user_editable or p.user_claimable)


def test_model_learned_internal_weights_cannot_be_claimed():
    for n in ("bedtime_penalty", "same_day_penalty", "sleep_target_penalty"):
        assert POLICY[n].model_learnable and not POLICY[n].user_claimable


def test_user_only_fields_are_never_model_learnable():
    for n in ("default_sleep_min_slots", "default_earliest_bed", "default_latest_bed",
              "reminders_enabled", "reminder_min_difficulty", "reminder_min_priority"):
        p = POLICY[n]
        assert p.default_tier == Tier.USER and p.user_editable and not p.model_learnable