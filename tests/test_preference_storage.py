import sqlite3

import pytest

from scheduler.db import (
    clear_evidence, connect, get_or_create_student, get_reflections, load_evidence,
    load_settings, load_tiers, log_reflection, save_evidence, save_settings, set_tier, transaction,
)
from scheduler.models import ProfileSettings
from scheduler.preference_policy import POLICY, Tier

OLD_SCHEMA = """  -- the pre-3.6 shapes of the three tables a legacy DB would have
CREATE TABLE students (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
CREATE TABLE profile_settings (student_id INTEGER PRIMARY KEY REFERENCES students(id),
    settings_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE reflections (id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL,
    created_at TEXT NOT NULL, reflection_text TEXT NOT NULL, settings_before_json TEXT NOT NULL,
    settings_after_json TEXT NOT NULL, applied INTEGER NOT NULL);
"""


@pytest.fixture
def conn():
    return connect(":memory:")


def test_new_student_gets_registry_default_tiers(conn):
    sid = get_or_create_student(conn, "Zane")
    tiers = load_tiers(conn, sid)
    assert set(tiers) == set(POLICY)
    assert all(tiers[n] == p.default_tier for n, p in POLICY.items())
    assert tiers["default_max_session_slots"] == Tier.USER
    assert tiers["buffer_slots"] == Tier.MODEL_LEARNED
    assert tiers["presence_bonus"] == Tier.LOCKED


def test_missing_tier_rows_are_backfilled_not_reset(conn):
    sid = get_or_create_student(conn, "Zane")
    load_tiers(conn, sid)
    set_tier(conn, sid, "buffer_slots", Tier.USER)
    conn.execute("DELETE FROM preference_tiers WHERE student_id=? AND field='bedtime_penalty'", (sid,))
    tiers = load_tiers(conn, sid)
    assert tiers["bedtime_penalty"] == Tier.MODEL_LEARNED   # restored from registry
    assert tiers["buffer_slots"] == Tier.USER                # existing choice untouched


def test_tiers_are_isolated_between_students(conn):
    a, b = get_or_create_student(conn, "A"), get_or_create_student(conn, "B")
    set_tier(conn, a, "buffer_slots", Tier.USER)
    assert load_tiers(conn, a)["buffer_slots"] == Tier.USER
    assert load_tiers(conn, b)["buffer_slots"] == Tier.MODEL_LEARNED


def test_evidence_round_trip_clear_and_isolation(conn):
    a, b = get_or_create_student(conn, "A"), get_or_create_student(conn, "B")
    save_evidence(conn, a, "buffer_slots", 2, "medium")
    save_evidence(conn, a, "bedtime_penalty", -1, "small")
    save_evidence(conn, a, "buffer_slots", 3, "small")            # overwrite, not duplicate
    assert load_evidence(conn, a) == {"buffer_slots": (3, "small"), "bedtime_penalty": (-1, "small")}
    assert load_evidence(conn, b) == {}
    clear_evidence(conn, a, "buffer_slots")
    assert set(load_evidence(conn, a)) == {"bedtime_penalty"}
    clear_evidence(conn, a)
    assert load_evidence(conn, a) == {}


def test_tiers_and_evidence_survive_close_and_reopen(tmp_path):
    path = str(tmp_path / "asg.db")
    c1 = connect(path)
    sid = get_or_create_student(c1, "Zane")
    set_tier(c1, sid, "buffer_slots", Tier.USER)
    save_evidence(c1, sid, "bedtime_penalty", 2, "medium")
    c1.close()

    c2 = connect(path)                                             # also proves connect() is idempotent
    assert load_tiers(c2, sid)["buffer_slots"] == Tier.USER
    assert load_evidence(c2, sid) == {"bedtime_penalty": (2, "medium")}


def test_legacy_database_opens_and_keeps_its_data(tmp_path):
    path = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA)
    raw.execute("INSERT INTO students VALUES (1, 'Old', '2026-01-01')")
    raw.execute("INSERT INTO profile_settings VALUES (1, '{\"buffer_slots\": 3}', '2026-01-01')")  # partial legacy JSON
    s = ProfileSettings().model_dump_json()
    raw.execute("INSERT INTO reflections VALUES (1, 1, '2026-01-02', 'rushed', ?, ?, 1)", (s, s))
    raw.commit()
    raw.close()

    conn = connect(path)                                           # no manual deletion needed
    assert load_settings(conn, 1).buffer_slots == 3                # old value kept
    assert load_settings(conn, 1).bedtime_penalty == ProfileSettings().bedtime_penalty  # missing -> default
    hist = get_reflections(conn, 1)
    assert len(hist) == 1 and hist[0].applied is True and hist[0].outcome is None
    tiers = load_tiers(conn, 1)
    assert all(tiers[n] == p.default_tier for n, p in POLICY.items())


def test_log_reflection_stores_outcome_and_defaults_to_none(conn):
    sid = get_or_create_student(conn, "Zane")
    st = load_settings(conn, sid)
    log_reflection(conn, sid, "a", before=st, after=st, applied=False)
    log_reflection(conn, sid, "b", before=st, after=st, applied=False, outcome="evidence_recorded")
    assert [r.outcome for r in get_reflections(conn, sid)] == [None, "evidence_recorded"]


def test_a_failed_transaction_rolls_back_everything_together(conn):
    sid = get_or_create_student(conn, "Zane")
    st = load_settings(conn, sid)
    with pytest.raises(RuntimeError):
        with transaction(conn):
            save_settings(conn, sid, st.model_copy(update={"buffer_slots": 4}))
            save_evidence(conn, sid, "buffer_slots", 1, "small")
            log_reflection(conn, sid, "x", before=st, after=st, applied=True)
            raise RuntimeError("boom")
    assert load_settings(conn, sid).buffer_slots == st.buffer_slots
    assert load_evidence(conn, sid) == {} and get_reflections(conn, sid) == []


# ---------- reads don't write, evidence is one read, reflection rows are typed ----------

def test_reading_tiers_writes_nothing(conn):
    sid = get_or_create_student(conn, "Zane")
    tiers = load_tiers(conn, sid)
    assert tiers == {name: p.default_tier for name, p in POLICY.items()}
    assert conn.execute("SELECT COUNT(*) FROM preference_tiers").fetchone() == (0,)
    set_tier(conn, sid, "buffer_slots", Tier.USER)  # only a real choice is stored
    assert load_tiers(conn, sid)["buffer_slots"] == Tier.USER
    assert conn.execute("SELECT COUNT(*) FROM preference_tiers").fetchone() == (1,)


def test_evidence_fresh_since_filters_by_when_it_last_changed(conn):
    from datetime import datetime, timezone
    sid = get_or_create_student(conn, "Zane")
    save_evidence(conn, sid, "buffer_slots", 2, "small", at="2026-10-01T09:00:00+00:00")
    save_evidence(conn, sid, "bedtime_penalty", -1, "large", at="2026-10-10T09:00:00")  # naive = UTC
    assert load_evidence(conn, sid) == {"buffer_slots": (2, "small"), "bedtime_penalty": (-1, "large")}
    since = datetime(2026, 10, 5, tzinfo=timezone.utc)
    assert load_evidence(conn, sid, fresh_since=since) == {"bedtime_penalty": (-1, "large")}
    assert load_evidence(conn, sid, fresh_since=since.replace(tzinfo=None)) == {"bedtime_penalty": (-1, "large")}


def test_live_evidence_reads_the_table_once(conn):
    from datetime import datetime, timezone
    from scheduler.preferences import live_evidence
    sid = get_or_create_student(conn, "Zane")
    save_evidence(conn, sid, "buffer_slots", 2, "small")
    reads = []
    conn.set_trace_callback(lambda sql: reads.append(sql) if "preference_evidence" in sql else None)
    assert live_evidence(conn, sid, datetime.now(timezone.utc)) == {"buffer_slots": (2, "small")}
    conn.set_trace_callback(None)
    assert len(reads) == 1


def test_reflection_rows_are_typed(conn):
    from scheduler.db import ReflectionRow
    sid = get_or_create_student(conn, "Zane")
    s = ProfileSettings()
    log_reflection(conn, sid, "felt rushed", before=s, after=s, applied=False, outcome="evidence_recorded")
    [row] = get_reflections(conn, sid)
    assert isinstance(row, ReflectionRow)
    assert (row.reflection_text, row.applied, row.outcome, row.settings_after) == ("felt rushed", False,
                                                                                    "evidence_recorded", s)
