import pytest
from scheduler.db import connect, get_or_create_student, load_approval_mode, get_reflections, load_evidence, load_settings
from scheduler.preference_policy import ApprovalMode, Tier
from scheduler.preferences import (EVIDENCE_THRESHOLD, Actor, Outcome, PreferenceError,
                                   change_tier, pending_approvals, process_reflection,
                                   resolve_pending, set_approval_mode)
from scheduler.reflection import PreferenceChangeProposal, ReflectionResult

@pytest.fixture
def conn(): return connect(":memory:")

@pytest.fixture
def sid(conn): return get_or_create_student(conn, "Zane")

def vote(conn, sid, direction="increase"):
    p = PreferenceChangeProposal(field="buffer_slots", direction=direction, magnitude="small", reason="t")
    return process_reflection(conn, sid, "t", ReflectionResult(summary="", proposals=[p]))

def ask_mode(conn, sid): set_approval_mode(conn, sid, ApprovalMode.ASK, Actor.USER)

def reach_pending(conn, sid, n=EVIDENCE_THRESHOLD):
    for _ in range(n): out = vote(conn, sid)
    return out

def test_ask_mode_holds_the_change_at_the_threshold(conn, sid):
    ask_mode(conn, sid)
    before = load_settings(conn, sid).buffer_slots
    out = reach_pending(conn, sid)
    assert out.outcome == Outcome.PENDING and load_settings(conn, sid).buffer_slots == before
    (p,) = pending_approvals(conn, sid)
    assert (p.field, p.old, p.new) == ("buffer_slots", before, before + 1)
    assert load_evidence(conn, sid)["buffer_slots"][0] == EVIDENCE_THRESHOLD

def test_approve_applies_exactly_one_bounded_step_and_clears_evidence(conn, sid):
    ask_mode(conn, sid)
    before = load_settings(conn, sid).buffer_slots
    reach_pending(conn, sid, EVIDENCE_THRESHOLD + 2)          # extra evidence must not stack
    assert resolve_pending(conn, sid, "buffer_slots", True).status == Outcome.APPROVED
    assert load_settings(conn, sid).buffer_slots == before + 1
    assert "buffer_slots" not in load_evidence(conn, sid) and pending_approvals(conn, sid) == []
    assert get_reflections(conn, sid)[-1].outcome == Outcome.APPROVED

def test_decline_keeps_value_clears_evidence_and_needs_fresh_evidence(conn, sid):
    ask_mode(conn, sid)
    before = load_settings(conn, sid).buffer_slots
    reach_pending(conn, sid)
    assert resolve_pending(conn, sid, "buffer_slots", False).status == Outcome.DECLINED
    assert load_settings(conn, sid).buffer_slots == before and load_evidence(conn, sid) == {}
    reach_pending(conn, sid, EVIDENCE_THRESHOLD - 1)
    assert pending_approvals(conn, sid) == []

def test_conflicting_evidence_cancels_a_pending_change(conn, sid):
    ask_mode(conn, sid); reach_pending(conn, sid)
    vote(conn, sid, "decrease")
    assert pending_approvals(conn, sid) == []

def test_pending_survives_closing_and_reopening_the_database(tmp_path):
    path = str(tmp_path / "a.db")
    c = connect(path); s = get_or_create_student(c, "Zane")
    ask_mode(c, s); reach_pending(c, s); c.close()
    c2 = connect(path)
    assert [p.field for p in pending_approvals(c2, s)] == ["buffer_slots"]
    assert load_approval_mode(c2, s) == ApprovalMode.ASK

def test_resolve_without_pending_is_rejected(conn, sid):
    ask_mode(conn, sid)
    with pytest.raises(PreferenceError, match="Nothing is waiting"):
        resolve_pending(conn, sid, "buffer_slots", True)

def test_claiming_the_field_clears_its_pending_change(conn, sid):
    ask_mode(conn, sid); reach_pending(conn, sid)
    change_tier(conn, sid, "buffer_slots", Tier.USER, Actor.USER)
    assert pending_approvals(conn, sid) == []

def test_mode_changes_are_validated_authorized_and_audited(conn, sid):
    with pytest.raises(PreferenceError):
        set_approval_mode(conn, sid, ApprovalMode.ASK, Actor.MODEL)
    with pytest.raises(PreferenceError, match="Unknown"):
        set_approval_mode(conn, sid, "sometimes", Actor.USER)
    assert set_approval_mode(conn, sid, ApprovalMode.ASK, Actor.USER) is True
    assert set_approval_mode(conn, sid, ApprovalMode.ASK, Actor.USER) is False   # no-op: no extra log row
    assert [r.outcome for r in get_reflections(conn, sid)] == [Outcome.APPROVAL_MODE]