
from scheduler.preferences import EVIDENCE_THRESHOLD
from tests.test_manual_review import run
from tests.test_preferences import buf
from scheduler.db import load_evidence, load_settings

def test_consistent_reflections_apply_exactly_one_bounded_update_then_reset(conn, sid):
    for _ in range(EVIDENCE_THRESHOLD - 1):
        assert run(conn, sid, ("buffer_slots", "increase", "medium")).outcome == "evidence_recorded"
    o = run(conn, sid, ("buffer_slots", "increase", "medium"))
    assert o.outcome == "learned_update_applied" and buf(conn, sid) == 1 + 2
    assert load_evidence(conn, sid) == {}
    assert run(conn, sid, ("buffer_slots", "increase", "medium")).outcome == "evidence_recorded"
    assert buf(conn, sid) == 3                                   # no accidental second change

def test_conflicting_evidence_restarts_the_streak(conn, sid):
    for d in ("increase", "increase", "decrease", "decrease"):
        run(conn, sid, ("buffer_slots", d, "small"))
    assert buf(conn, sid) == 1                                   # streak restarted at the first decrease
    run(conn, sid, ("buffer_slots", "decrease", "small"))
    assert buf(conn, sid) == 0

def test_user_owned_field_gets_no_evidence_and_no_change(conn, sid):
    for _ in range(4):
        o = run(conn, sid, ("default_max_session_slots", "decrease", "large"))
        assert o.outcome == "proposal_ignored"
    assert load_evidence(conn, sid) == {} and load_settings(conn, sid).default_max_session_slots == 8