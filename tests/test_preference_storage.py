
from scheduler.db import connect, get_or_create_student, load_tiers, save_evidence, set_tier
from scheduler.preference_policy import POLICY, Tier
from scheduler.reflection import log_reflection, load_settings, get_reflections, save_settings
from scheduler.models import ProfileSettings
from scheduler.reflection_cycle import load_evidence

def test_new_student_gets_registry_default_tiers(conn):
    sid = get_or_create_student(conn, "Zane")
    tiers = load_tiers(conn, sid)
    assert all(tiers[n] == p.default_tier for n, p in POLICY.items())
    assert tiers["default_max_session_slots"] == Tier.USER

def test_tiers_and_evidence_survive_close_and_reopen(tmp_path):
    path = str(tmp_path / "asg.db")
    c1 = connect(path); sid = get_or_create_student(c1, "Zane")
    set_tier(c1, sid, "buffer_slots", Tier.USER)
    save_evidence(c1, sid, "bedtime_penalty", 2, "medium"); c1.close()
    c2 = connect(path)  # also proves connect() is idempotent
    assert load_tiers(c2, sid)["buffer_slots"] == Tier.USER
    assert load_evidence(c2, sid) == {"bedtime_penalty": (2, "medium")}

def test_commit_false_lets_callers_roll_back_everything_together(conn):
    sid = get_or_create_student(conn, "Zane"); st = load_settings(conn, sid)
    save_settings(conn, sid, st.model_copy(update={"buffer_slots": 4}), commit=False)
    save_evidence(conn, sid, "buffer_slots", 1, "small", commit=False)
    log_reflection(conn, sid, "x", before=st, after=st, applied=True, commit=False)
    conn.rollback()
    assert load_settings(conn, sid).buffer_slots == st.buffer_slots
    assert load_evidence(conn, sid) == {} and get_reflections(conn, sid) == []