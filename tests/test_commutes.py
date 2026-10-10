from datetime import date
import pytest
from pydantic import ValidationError
from scheduler.commutes import commute_overlaps, commute_to_block, expand_commutes, overlap_warnings
from scheduler.models import Commute, FixedBlock, PlanAnchor

MON = date(2026, 10, 5)  # a Monday

def anchor(days=14):
    return PlanAnchor(start_date=MON, num_days=days)

def recurring(**kw):
    base = dict(start_time="07:00", length_minutes=45, recurring=True, weekday="Mon")
    return Commute(**{**base, **kw})

def test_fixedblock_buffer_before_defaults_true():
    assert FixedBlock(title="x", start_slot=0, end_slot=1).buffer_before is True

def test_recurring_expands_only_matching_weekday():
    blocks = expand_commutes([recurring()], anchor(14))
    assert [b.day for b in blocks] == [0, 7]
    assert all(b.buffer_before is False for b in blocks)

def test_one_time_expands_once():
    c = Commute(start_time="07:00", length_minutes=30, date="2026-10-07")
    assert [b.day for b in expand_commutes([c], anchor())] == [2]

def test_one_time_outside_window_gives_nothing():
    c = Commute(start_time="07:00", length_minutes=30, date="2027-01-01")
    assert expand_commutes([c], anchor()) == []

def test_end_date_stops_recurrence():
    assert [b.day for b in expand_commutes([recurring(end_date="2026-10-05")], anchor(14))] == [0]

def test_skip_date_removes_one_occurrence():
    assert [b.day for b in expand_commutes([recurring(skip_dates=["2026-10-12"])], anchor(14))] == [0]

def test_two_commutes_same_day():
    blocks = expand_commutes([recurring(), recurring(start_time="16:30", length_minutes=60)], anchor(7))
    assert sorted((b.day, b.start_slot) for b in blocks) == [(0, 28), (0, 66)]

def test_rounds_outward_to_slots():
    b = commute_to_block(recurring(start_time="07:10", length_minutes=45), 0)
    assert (b.start_slot, b.end_slot) == (28, 32)

def test_commute_can_cross_midnight():
    b = commute_to_block(recurring(start_time="23:30", length_minutes=60), 0)
    assert (b.start_slot, b.end_slot) == (94, 98)

def test_overlap_is_reported_not_dropped():
    cls = FixedBlock(title="Class", day=0, start_slot=30, end_slot=40)  # 07:30-10:00
    blocks = [commute_to_block(recurring(), 0)]  # 07:00-07:45
    pairs = commute_overlaps([cls], blocks)
    assert pairs == [(blocks[0], cls)]
    w = overlap_warnings(pairs, anchor())
    assert w[0].kind == "commute_overlap" and w[0].severity == "soft"
    assert "Mon 05 Oct" in w[0].message and "Class" in w[0].message

def test_back_to_back_is_not_an_overlap():
    cls = FixedBlock(title="Class", day=0, start_slot=32, end_slot=40)
    assert commute_overlaps([cls], [commute_to_block(recurring(), 0)]) == []

def test_commute_vs_commute_reported_once():
    a = commute_to_block(recurring(), 0)
    b = commute_to_block(recurring(start_time="07:30"), 0)
    assert len(commute_overlaps([], [a, b])) == 1

@pytest.mark.parametrize("kw", [
    dict(recurring=True),
    dict(recurring=False),
    dict(recurring=False, date="2026-10-05", weekday="Mon"),
    dict(recurring=True, weekday="Mon", date="2026-10-05"),
    dict(recurring=True, weekday="Mon", end_date="not-a-date"),
    dict(recurring=True, weekday="Mon", start_time="25:00"),
    dict(recurring=True, weekday="Mon", length_minutes=0),
])
def test_invalid_commutes_rejected(kw):
    with pytest.raises((ValidationError, ValueError)):
        Commute(**{**dict(start_time="07:00", length_minutes=30), **kw})
def test_commute_dates_and_times_use_the_shared_checks():
    # "20261005" used to pass unchecked and never match a plan day (#15)
    c = Commute(start_time="7:05", length_minutes=30, date="20261005")
    assert (c.start_time, c.date) == ("07:05", "2026-10-05")
    assert [b.day for b in expand_commutes([c], anchor())] == [0]
    r = recurring(end_date="20261012", skip_dates=["20261005"])
    assert (r.end_date, r.skip_dates) == ("2026-10-12", ["2026-10-05"])
    assert [b.day for b in expand_commutes([r], anchor(14))] == [7]
    for bad in (dict(start_time="25:00"), dict(start_time="7.30"), dict(date="2026-02-30")):
        with pytest.raises(ValidationError):
            Commute(**{"start_time": "07:00", "length_minutes": 30, "date": "2026-10-05", **bad})


def test_skipping_a_commute_date_stores_the_normalised_date():
    from scheduler.db import COMMUTES, connect, get_or_create_student, skip_commute_date
    conn = connect(":memory:")
    sid = get_or_create_student(conn, "Z")
    cid = COMMUTES.add(conn, sid, recurring())
    assert skip_commute_date(conn, sid, cid, "2026-10-12") is True
    assert skip_commute_date(conn, sid, cid, "20261012") is True  # the same day: no duplicate
    assert COMMUTES.get(conn, sid)[0][1].skip_dates == ["2026-10-12"]


def test_commute_overlap_warnings_read_like_class_overlaps_and_are_capped():
    """One overlap format for the student: quoted titles, and at most five lines plus a count."""
    cls = FixedBlock(title="Class", day=0, start_slot=30, end_slot=40)
    commute = commute_to_block(recurring(), 0)  # 07:00-07:45
    [w] = overlap_warnings([(commute, cls)], anchor())
    assert w.message == "Mon 05 Oct: 'Commute' 07:00-07:45 overlaps 'Class' 07:30-10:00 (both kept; tasks avoid both)"
    many = overlap_warnings([(commute, cls)] * 7, anchor())
    assert len(many) == 6 and many[-1].message.startswith("...and 2 more overlap(s)")
