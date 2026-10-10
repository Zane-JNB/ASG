
# tests/test_main_flows.py -- top of file
import subprocess, sys   
from collections import Counter   
from datetime import date, datetime   

import pytest   

from scheduler.add_with_fit import add_task_with_fit   
from scheduler.db import DATED_BLOCKS, EXTRACTED_TASKS, connect, get_or_create_student, get_plan_cuts
from scheduler.models import DatedBlock, ExtractedTask   
from scheduler.planner import plan_from_saved   
from scheduler.restore import plan_restores   
from scheduler.task_manager import run_menu   

D = date(2026, 10, 5)   
NOW = datetime(2026, 10, 5, 9, 0)   
NEW_ESSAY = ["a", "Essay", "2026-10-05", "4", "5", "3", ""]   


@pytest.fixture   
def db(tmp_path):
    return str(tmp_path / "asg.db")


@pytest.fixture   
def conn(db):
    c = connect(db)
    sid = get_or_create_student(c, "Zane")
    DATED_BLOCKS.add(c, sid, DatedBlock(title="Class", date=D.isoformat(), start_time="09:00", end_time="17:00"))
    EXTRACTED_TASKS.add(c, sid, ExtractedTask(title="Lab", date=D.isoformat(), duration_slots=20, priority=4, difficulty=3))
    return c


def planned(conn):  #    {title: slots} actually placed on the timetable
    _, _, items, warns = plan_from_saved(conn, 1, now=NOW, time_limit_seconds=10)
    out = Counter()
    for i in items:
        if i.kind == "task":
            out[i.title.split(" (")[0]] += i.end_slot - i.start_slot
    return out, warns

def menu(conn, answers):   
    it, shown = iter(answers + ["q"]), []
    def ask(p):
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(f"menu asked for more input: {p!r}")
    run_menu(conn, 1, ask=ask, show=shown.append, now=NOW)
    return shown

def test_1_manual_drop_then_save_end_to_end(conn):   
    menu(conn, NEW_ESSAY + ["m", "1", "d", "s"])
    slots, warns = planned(conn)
    assert slots["Essay"] == 16 and "Lab" not in slots
    assert {t.title: t.duration_slots for _, t in EXTRACTED_TASKS.get(conn, 1)}["Lab"] == 20
    assert not [w for w in warns if w.kind == "task_unscheduled"]

def test_2_manual_cuts_survive_a_restart(conn, db):   
    menu(conn, NEW_ESSAY + ["m", "1", "d", "s"])
    conn.close()
    slots, _ = planned(connect(db))
    assert slots["Essay"] == 16 and "Lab" not in slots

def test_3_manual_dont_add_leaves_the_timetable_exactly_as_before(conn):   
    before, _ = planned(conn)
    menu(conn, NEW_ESSAY + ["m", "1", "d", "n"])
    after, _ = planned(conn)
    assert after == before and get_plan_cuts(conn, 1) == {}

def test_4_manual_cancel_after_cutting_restores_the_timetable(conn):   
    before, _ = planned(conn)
    menu(conn, NEW_ESSAY + ["m", "1", "d", "", "y", ""])
    assert planned(conn)[0] == before and len(EXTRACTED_TASKS.get(conn, 1)) == 1

def test_5_manual_time_reduction_keeps_the_task_but_shorter(conn):   
    menu(conn, NEW_ESSAY + ["m", "1", "t", "4", "s"])
    slots, _ = planned(conn)
    assert slots["Lab"] == 4 and slots["Essay"] == 16

def test_6_semi_pick_one_makes_everything_fit(conn):   
    shown = menu(conn, NEW_ESSAY + ["s", "1"])
    assert any("Options, best first" in l for l in shown)
    slots, warns = planned(conn)
    assert slots["Essay"] > 0 and not [w for w in warns if w.kind == "task_unscheduled"]

def test_7_automatic_y_applies_best_plan_and_n_applies_nothing(conn):   
    menu(conn, NEW_ESSAY + ["a", "n", ""])
    assert get_plan_cuts(conn, 1) == {} and len(EXTRACTED_TASKS.get(conn, 1)) == 1
    menu(conn, NEW_ESSAY + ["a", "y"])
    assert sum(get_plan_cuts(conn, 1).values()) > 0 and len(EXTRACTED_TASKS.get(conn, 1)) == 2

def test_8_a_bad_mode_letter_is_rejected_and_nothing_is_saved(conn):   
    shown = menu(conn, NEW_ESSAY + ["x", ""])
    assert "Choose m, s or a." in shown and len(EXTRACTED_TASKS.get(conn, 1)) == 1

def test_9_no_mode_prompt_when_the_task_already_fits(conn):   
    menu(conn, ["a", "Quiz", "2026-10-09", "1", "3", "3"])
    assert len(EXTRACTED_TASKS.get(conn, 1)) == 2 and get_plan_cuts(conn, 1) == {}

def test_10_cut_time_is_offered_back_once_room_appears(conn):   
    from scheduler.task_manager import _sorted_tasks
    menu(conn, NEW_ESSAY + ["m", "1", "d", "s"])
    n = 1 + [t.title for _, t in _sorted_tasks(conn, 1)].index("Essay")
    menu(conn, ["d", str(n)])
    lab_id = next(i for i, t in EXTRACTED_TASKS.get(conn, 1) if t.title == "Lab")
    assert plan_restores(conn, 1, NOW) == {lab_id: 20}

def test_11_must_add_hides_dont_add_in_manual(conn):   
    shown, it = [], iter(["m", "1", "d", "s"])
    new = ExtractedTask(title="Essay", date=D.isoformat(), duration_slots=16, priority=5, difficulty=3)
    add_task_with_fit(conn, 1, new, NOW, ask=lambda p: (shown.append(p), next(it))[1], show=shown.append, must_add=True)
    assert not any("don't add" in s for s in shown if isinstance(s, str))

def test_12_main_py_runs_offline_and_shows_all_three_modes():   
    out = subprocess.run([sys.executable, "main.py"], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    for marker in ("MANUAL", "SEMI-AUTOMATIC", "AUTOMATIC"):
        assert marker in out.stdout
    assert "Reflection skipped (Groq call failed)" in out.stdout  # conftest removed the key