import pytest
from scheduler.models import ProfileSettings
from scheduler.preference_policy import MAGNITUDES, MODEL_DELTAS, POLICY, ApprovalMode, FieldPolicy, Kind, Tier

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

@pytest.mark.parametrize("kind,kwargs", [
    (Kind.LOCKED, dict(deltas={"small": 1})),     # deltas on a field nothing learns
    (Kind.USER_ONLY, dict(deltas={"small": 1})),
    (Kind.INTERNAL, dict()),                       # learnable, but no deltas to learn with
    (Kind.CLAIMABLE, dict()),
    (Kind.INTERNAL, dict(claimed=True)),           # only a claimable field can start claimed
])
def test_invalid_policies_are_rejected(kind, kwargs):
    with pytest.raises(ValueError):
        FieldPolicy(kind, "x", "y", **kwargs)


@pytest.mark.parametrize("kind,tier,learnable,editable,claimable", [
    (Kind.LOCKED, Tier.LOCKED, False, False, False),
    (Kind.USER_ONLY, Tier.USER, False, True, False),
    (Kind.INTERNAL, Tier.MODEL_LEARNED, True, False, False),
    (Kind.CLAIMABLE, Tier.MODEL_LEARNED, True, True, True),
])
def test_a_kind_decides_who_may_change_a_field(kind, tier, learnable, editable, claimable):
    p = FieldPolicy(kind, "x", "y", deltas={"small": 1} if learnable else None)
    assert (p.default_tier, p.model_learnable, p.user_editable, p.user_claimable) == (tier, learnable, editable, claimable)


def test_a_claimed_field_starts_as_the_students():
    assert FieldPolicy(Kind.CLAIMABLE, "x", "y", deltas={"small": 1}, claimed=True).default_tier == Tier.USER


def test_tier_serialized_values_are_exact():
    assert [t.value for t in Tier] == ["locked", "user", "model_learned"]


def test_approval_mode_and_magnitude_values_are_exact():
    assert [m.value for m in ApprovalMode] == ["auto", "ask"]
    assert MAGNITUDES == ("small", "medium", "large")


def test_adjustable_fields_unchanged_by_the_refactor():   # replace the short version
    assert MODEL_DELTAS == {
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