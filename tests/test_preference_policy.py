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
    with pytest.raises(ValueError):
        FieldPolicy(label="x", description="y", **kwargs)

def test_adjustable_fields_unchanged_by_the_refactor():  # compare to your old dict's 7 entries
    assert ADJUSTABLE_FIELDS == MODEL_DELTAS
    assert set(ADJUSTABLE_FIELDS) == {"buffer_slots", "bedtime_penalty", "same_day_penalty",
        "default_max_session_slots", "sleep_target_penalty", "default_sleep_length_slots", "default_preferred_bed"}