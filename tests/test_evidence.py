from types import SimpleNamespace

import pytest

from scheduler.db import (
    connect, get_or_create_student, get_reflections, load_evidence, load_settings, load_tiers,
)
from scheduler.preference_policy import Tier
from scheduler.preferences import (
    EVIDENCE_THRESHOLD, Actor, change_tier, collapse_proposals, learnable_fields, next_evidence,
    process_reflection, set_values,
)
from scheduler.reflection import (
    PreferenceChangeProposal as P, ReflectionResult, build_system_prompt, propose_preference_changes,
)


@pytest.fixture
def conn():
    return connect(":memory:")


@pytest.fixture
def sid(conn):
    return get_or_create_student(conn, "Zane")


def refl(*items):
    """items: (field, direction, magnitude) tuples -> ReflectionResult."""
    return ReflectionResult(summary="", proposals=[
        P(field=f, direction=d, magnitude=m, reason="r") for f, d, m in items])


def raw(*proposals):
    """Bypass ReflectionResult's own validation to test the engine's independent re-check."""
    return SimpleNamespace(summary="", proposals=list(proposals))


def run(conn, sid, *items):
    return process_reflection(conn, sid, "text", refl(*items))


def buf(conn, sid):
    return load_settings(conn, sid).buffer_slots


# --- pure scoring ---
def test_next_evidence_accumulates_cancels_on_conflict_and_keeps_smallest_bucket():
    assert next_evidence(None, "increase", "large") == (1, "large")
    assert next_evidence((1, "large"), "increase", "small") == (2, "small")
    assert next_evidence((2, "small"), "increase", "large") == (3, "small")
    assert next_evidence((2, "small"), "decrease", "large") == (1, "small")       #  cancels one vote, not restart
    assert next_evidence((-2, "medium"), "increase", "small") == (-1, "medium")   #  mirror case, keeps prior bucket
    assert next_evidence((1, "small"), "decrease", "small")[0] == 0               #  fully cancelled

def test_collapse_duplicates_and_contradictions():
    ps = refl(("buffer_slots", "increase", "large"), ("buffer_slots", "increase", "small"),
              ("bedtime_penalty", "increase", "small"), ("bedtime_penalty", "decrease", "small")).proposals
    assert collapse_proposals(ps) == {"buffer_slots": ("increase", "small"), "bedtime_penalty": None}


# --- threshold behaviour ---
def test_one_reflection_never_changes_a_setting(conn, sid):
    o = run(conn, sid, ("buffer_slots", "increase", "large"))
    assert o.outcome == "evidence_recorded" and buf(conn, sid) == 1
    assert load_evidence(conn, sid) == {"buffer_slots": (1, "large")}


def test_consistent_reflections_apply_exactly_one_bounded_update_then_reset(conn, sid):
    for _ in range(EVIDENCE_THRESHOLD - 1):
        assert run(conn, sid, ("buffer_slots", "increase", "medium")).outcome == "evidence_recorded"
    o = run(conn, sid, ("buffer_slots", "increase", "medium"))
    assert o.outcome == "learned_update_applied" and buf(conn, sid) == 1 + 2    # one 'medium' delta
    assert load_evidence(conn, sid) == {}                                       # score reset
    assert run(conn, sid, ("buffer_slots", "increase", "medium")).outcome == "evidence_recorded"
    assert buf(conn, sid) == 3                                                  # no accidental 2nd change


def test_applies_smallest_bucket_seen_in_the_streak(conn, sid):
    for mag in ("large", "small", "large"):
        run(conn, sid, ("buffer_slots", "increase", mag))
    assert buf(conn, sid) == 1 + 1


def test_unmentioned_field_keeps_its_evidence(conn, sid):
    run(conn, sid, ("buffer_slots", "increase", "small"))
    run(conn, sid, ("bedtime_penalty", "increase", "small"))
    assert load_evidence(conn, sid)["buffer_slots"] == (1, "small")


def test_duplicate_same_field_proposals_count_once(conn, sid):
    run(conn, sid, *[("buffer_slots", "increase", "small")] * 5)
    assert load_evidence(conn, sid) == {"buffer_slots": (1, "small")}


def test_contradictory_duplicates_in_one_reflection_are_skipped(conn, sid):
    run(conn, sid, ("buffer_slots", "increase", "small"))
    o = run(conn, sid, ("buffer_slots", "increase", "small"), ("buffer_slots", "decrease", "small"))
    assert o.outcome == "proposal_ignored"
    assert load_evidence(conn, sid) == {"buffer_slots": (1, "small")}           # untouched


def test_evidence_survives_close_and_reopen(tmp_path):
    path = str(tmp_path / "asg.db")
    c = connect(path); s = get_or_create_student(c, "Zane")
    run(c, s, ("buffer_slots", "increase", "small")); run(c, s, ("buffer_slots", "increase", "small"))
    c.close()
    c = connect(path)
    assert run(c, s, ("buffer_slots", "increase", "small")).outcome == "learned_update_applied"


def test_evidence_and_updates_are_isolated_between_students(conn, sid):
    other = get_or_create_student(conn, "Other")
    for _ in range(3):
        run(conn, sid, ("buffer_slots", "increase", "small"))
    assert buf(conn, sid) == 2 and buf(conn, other) == 1 and load_evidence(conn, other) == {}


# --- authorization ---
def test_user_owned_field_gets_no_evidence_and_no_change(conn, sid):
    assert load_tiers(conn, sid)["default_max_session_slots"] == Tier.USER
    for _ in range(4):
        o = run(conn, sid, ("default_max_session_slots", "decrease", "large"))
        assert o.outcome == "proposal_ignored" and "set by you" in o.results[0].message
    assert load_evidence(conn, sid) == {} and load_settings(conn, sid).default_max_session_slots == 8


def test_claiming_a_field_stops_accumulation_and_clears_evidence(conn, sid):
    run(conn, sid, ("buffer_slots", "increase", "small"))
    change_tier(conn, sid, "buffer_slots", Tier.USER, Actor.USER)
    assert load_evidence(conn, sid) == {}
    assert run(conn, sid, ("buffer_slots", "increase", "small")).outcome == "proposal_ignored"
    assert load_evidence(conn, sid) == {}


def test_returning_a_field_to_the_model_starts_with_clean_evidence(conn, sid):
    set_values(conn, sid, {"default_max_session_slots": 6}, Actor.USER)
    change_tier(conn, sid, "default_max_session_slots", Tier.MODEL_LEARNED, Actor.USER)
    run(conn, sid, ("default_max_session_slots", "decrease", "small"))
    assert load_evidence(conn, sid) == {"default_max_session_slots": (-1, "small")}
    assert load_settings(conn, sid).default_max_session_slots == 6


def test_locked_fields_reject_model_evidence_both_ways(conn, sid):
    change_tier(conn, sid, "buffer_slots", Tier.LOCKED, Actor.INTERNAL)          # tier-locked
    assert run(conn, sid, ("buffer_slots", "increase", "small")).results[0].message.endswith("protected, so it wasn't changed.")
    bypass = P.model_construct(field="presence_bonus", direction="increase", magnitude="large", reason="x")
    o = process_reflection(conn, sid, "t", raw(bypass))                           # policy-locked
    assert o.outcome == "proposal_ignored"
    assert load_evidence(conn, sid) == {} and load_settings(conn, sid).presence_bonus == 10_000


def test_invalid_and_unauthorized_proposals_dont_block_valid_ones(conn, sid):
    unknown = P.model_construct(field="made_up", direction="increase", magnitude="small", reason="x")
    ps = refl(("default_max_session_slots", "decrease", "small"), ("buffer_slots", "increase", "small")).proposals
    o = process_reflection(conn, sid, "t", raw(unknown, *ps))
    assert o.outcome == "evidence_recorded"
    assert {r.field: r.status for r in o.results} == {
        "made_up": "proposal_ignored", "default_max_session_slots": "proposal_ignored",
        "buffer_slots": "evidence_recorded"}
    assert load_evidence(conn, sid) == {"buffer_slots": (1, "small")}


# --- bounds / safety ---
def test_threshold_respects_lower_bound_and_clamps(conn, sid):
    for _ in range(3):
        run(conn, sid, ("bedtime_penalty", "decrease", "large"))                 # 50 - 50 -> clamped to 1
    assert load_settings(conn, sid).bedtime_penalty == 1
    set_values(conn, sid, {"buffer_slots": 0}, Actor.INTERNAL)
    for _ in range(2):
        run(conn, sid, ("buffer_slots", "decrease", "small"))
    o = run(conn, sid, ("buffer_slots", "decrease", "small"))
    assert o.outcome == "threshold_at_limit" and buf(conn, sid) == 0 and load_evidence(conn, sid) == {}


def test_threshold_respects_upper_bound(conn, sid):
    set_values(conn, sid, {"default_earliest_bed": 84, "default_preferred_bed": 190,
                           "default_latest_bed": 191}, Actor.INTERNAL)
    for _ in range(3):
        run(conn, sid, ("default_preferred_bed", "increase", "large"))           # 190+8 clamps to 191
    assert load_settings(conn, sid).default_preferred_bed == 191


def test_model_cannot_push_sleep_target_below_users_minimum_or_bed_past_window(conn, sid):
    set_values(conn, sid, {"default_sleep_min_slots": 30}, Actor.USER)                           # target is 32
    for _ in range(3):
        o = run(conn, sid, ("default_sleep_length_slots", "decrease", "large"))   # 32-8 < min 30
    assert o.outcome == "threshold_at_limit" and load_settings(conn, sid).default_sleep_length_slots == 32
    for _ in range(3):
        o = run(conn, sid, ("default_preferred_bed", "increase", "large"))        # 92+8 = 100 ok (== latest)
    for _ in range(3):
        o = run(conn, sid, ("default_preferred_bed", "increase", "small"))        # 102 > latest 100
    assert load_settings(conn, sid).default_preferred_bed == 100 and o.outcome == "threshold_at_limit"
    load_settings(conn, sid).default_sleep_rule(0)                                # still a valid rule


# --- audit + filtering ---
def test_outcomes_are_logged_per_reflection(conn, sid):
    run(conn, sid)                                                                # no proposals
    run(conn, sid, ("default_max_session_slots", "decrease", "small"))            # ignored
    for _ in range(3):
        run(conn, sid, ("buffer_slots", "increase", "small"))
    log = get_reflections(conn, sid)
    assert [r.outcome for r in log] == ["no_proposals", "proposal_ignored",
                                           "evidence_recorded", "evidence_recorded", "learned_update_applied"]
    assert [r.applied for r in log] == [False] * 4 + [True]
    assert log[-1].settings_before.buffer_slots == 1 and log[-1].settings_after.buffer_slots == 2


def test_prompt_and_schema_only_show_learnable_fields(conn, sid):
    change_tier(conn, sid, "buffer_slots", Tier.USER, Actor.USER)
    fields = learnable_fields(conn, sid)
    assert "buffer_slots" not in fields and "default_max_session_slots" not in fields
    assert "bedtime_penalty" in fields
    prompt = build_system_prompt(fields)
    assert "bedtime_penalty" in prompt and "- buffer_slots" not in prompt
    assert "- buffer_slots" in build_system_prompt()                              # default unchanged


def test_no_llm_call_when_nothing_is_learnable():
    class Boom:
        class messages:
            @staticmethod
            def create(**kw):
                raise AssertionError("LLM must not be called")
    r = propose_preference_changes("felt rushed", client=Boom(), allowed_fields=[])
    assert r.proposals == []

def test_next_evidence_is_a_dial():
    assert next_evidence(None, "increase", "small") == (1, "small")
    assert next_evidence((2, "small"), "decrease", "large") == (1, "small")   # cancels one vote, keeps cautious magnitude
    assert next_evidence((1, "small"), "decrease", "small")[0] == 0
    assert next_evidence((2, "medium"), "increase", "small") == (3, "small")

def test_conflicting_evidence_cancels_one_vote_at_a_time(conn, sid):             # (replaces ..._restarts_the_streak)
    for d in ("increase", "increase", "decrease"):
        run(conn, sid, ("buffer_slots", d, "small"))
    assert load_evidence(conn, sid)["buffer_slots"][0] == 1                      # +1, +2, +1
    run(conn, sid, ("buffer_slots", "decrease", "small"))
    assert load_evidence(conn, sid) == {} and buf(conn, sid) == 1                # fully cancelled, nothing applied

def test_cancel_delays_but_does_not_block_a_real_streak(conn, sid):
    for d in ("increase", "increase", "decrease", "increase", "increase"):
        run(conn, sid, ("buffer_slots", d, "small"))
    assert buf(conn, sid) == 2                                                   # fired on the 5th reflection