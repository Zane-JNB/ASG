import sqlite3
from datetime import datetime, timedelta, timezone
import pytest
from scheduler.db import connect, get_or_create_student, load_evidence, load_settings
from scheduler.preferences import (APPROVAL_ASK, Actor, PreferenceError, pending_approvals, process_reflection,
                                   resolve_pending, set_approval_mode)
from scheduler.reflection import PreferenceChangeProposal, ReflectionResult

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
def day(n): return T0 + timedelta(days=n)

@pytest.fixture
def conn(): return connect(":memory:")

@pytest.fixture
def sid(conn): return get_or_create_student(conn, "Zane")

def vote_at(conn, sid, t, direction="increase", proposals=True):
    ps = [PreferenceChangeProposal(field="buffer_slots", direction=direction, magnitude="small", reason="t")] if proposals else []
    return process_reflection(conn, sid, "t", ReflectionResult(summary="", proposals=ps), now=t)

def score(conn, sid): return load_evidence(conn, sid).get("buffer_slots", (0,))[0]

def test_evidence_just_under_14_idle_days_still_counts(conn, sid):
    vote_at(conn, sid, T0); vote_at(conn, sid, T0 + timedelta(days=13, hours=23))
    assert score(conn, sid) == 2

def test_evidence_idle_for_exactly_14_days_expires_and_restarts(conn, sid):
    vote_at(conn, sid, T0); vote_at(conn, sid, day(14))
    assert score(conn, sid) == 1                              # fresh streak, not 2

def test_each_vote_restarts_the_idle_clock(conn, sid):
    before = load_settings(conn, sid).buffer_slots
    for n in (0, 10, 20):                                     # 10 idle days each time: never expires
        out = vote_at(conn, sid, day(n))
    assert out.outcome == "learned_update_applied"
    assert load_settings(conn, sid).buffer_slots == before + 1

def test_a_cancelling_vote_also_counts_as_activity(conn, sid):
    vote_at(conn, sid, day(0)); vote_at(conn, sid, day(1))    # +2
    vote_at(conn, sid, day(12), "decrease")                   # +1, clock restarts at day 12
    vote_at(conn, sid, day(25))                               # 13 idle days: still live
    assert score(conn, sid) == 2

def test_expired_rows_are_purged_by_the_next_reflection(conn, sid):
    vote_at(conn, sid, T0)
    vote_at(conn, sid, day(15), proposals=False)
    assert load_evidence(conn, sid) == {}

def test_pending_approval_also_expires_when_idle(conn, sid):
    set_approval_mode(conn, sid, APPROVAL_ASK, Actor.USER)
    for _ in range(3): vote_at(conn, sid, T0)
    assert [p.field for p in pending_approvals(conn, sid, now=day(13))] == ["buffer_slots"]
    assert pending_approvals(conn, sid, now=day(14)) == []
    with pytest.raises(PreferenceError):
        resolve_pending(conn, sid, "buffer_slots", True, now=day(14))

def test_legacy_evidence_table_gains_updated_at_and_rows_stay_live(tmp_path):
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE preference_evidence (student_id INTEGER NOT NULL, field TEXT NOT NULL, "
                "score INTEGER NOT NULL, magnitude TEXT NOT NULL, PRIMARY KEY (student_id, field))")
    raw.execute("INSERT INTO preference_evidence VALUES (1, 'buffer_slots', 2, 'small')")
    raw.commit(); raw.close()
    conn = connect(path)
    assert conn.execute("SELECT updated_at FROM preference_evidence WHERE field = 'buffer_slots'").fetchone()[0]
    assert load_evidence(conn, 1) == {"buffer_slots": (2, "small")}