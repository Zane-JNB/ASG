from datetime import date, datetime, time

import pytest

from scheduler.add_with_fit import add_task_with_fit, apply_drop_choice
from scheduler.db import (
    DATED_BLOCKS, EXTRACTED_TASKS, connect, get_or_create_student, get_plan_cuts, load_settings,
    save_settings,
)
from scheduler.make_room import describe_proposal
from scheduler.dropping import _options, _shrink_amounts, propose_drops
from scheduler.fit_check import build_fit_inputs
from scheduler.import_flow import run_import
from scheduler.models import (
    DatedBlock, DynamicTask, ExtractedTask, ExtractionResult, FixedBlock,
    ProfileSettings, SleepRule,
)
from scheduler.prompts import describe_task
from scheduler.review import review_extraction
from scheduler.solver import PlanFrame
from scheduler.task_manager import prompt_new_task, run_menu


def _planned(conn, sid, day):
    """[(saved task id, task)] as planned from midnight that day, plan cuts applied."""
    return build_fit_inputs(conn, sid, datetime.combine(day, time())).planned


@pytest.fixture(autouse=True)
def _import_clock_before_the_sample_dates(monkeypatch):
    """run_import asks about tasks already due; these samples are dated Oct 2026, so pin 'now' before them."""
    class _Before(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 1, 9, 0)
    monkeypatch.setattr("scheduler.import_flow.datetime", _Before)


D = date(2026, 10, 5)
NOW = datetime(2026, 10, 5, 9, 0)


def scripted(answers):
    it, shown = iter(answers), []
    return (lambda _p: next(it)), shown


@pytest.fixture
def conn():
    return connect(":memory:")


@pytest.fixture
def sid(conn):
    return get_or_create_student(conn, "Zane")


def _tight_day(conn, sid):
    """Class 09-17, a 5h one-block Lab, and a 4h one-block Essay due today: it cannot all fit."""
    DATED_BLOCKS.add(conn, sid, DatedBlock(title="Class", date=D.isoformat(), start_time="09:00", end_time="17:00"))
    EXTRACTED_TASKS.add(conn, sid, ExtractedTask(title="Lab", date=D.isoformat(), duration_slots=20,
                                                 priority=4, difficulty=3, splittable=False))
    return ExtractedTask(title="Essay", date=D.isoformat(), duration_slots=16, priority=5,
                         difficulty=3, splittable=False)


# ---------- shortening a one-block task ----------
def test_shrink_amounts_never_reach_a_full_drop():
    s = ProfileSettings()
    assert _shrink_amounts(4, s) == [1, 2, 3]
    assert _shrink_amounts(2, s) == [1]
    assert _shrink_amounts(1, s) == []


def test_only_one_block_tasks_can_be_shortened():
    s = ProfileSettings()
    one = DynamicTask(title="Lab", duration_slots=20, priority=3, difficulty=3, splittable=False)
    many = DynamicTask(title="Project", duration_slots=40, priority=3, difficulty=3, max_session_slots=8)
    opts = _options([one, many], s)
    assert any(a.shrink for a in opts[0]) and not any(a.shrink for a in opts[1])
    assert all(not a.shrink for a in opts[1])


def test_a_shortened_task_is_still_one_block_in_the_verified_schedule():
    fixed = [FixedBlock(title="Class", start_slot=32, end_slot=48)]
    lab = DynamicTask(title="Lab", duration_slots=30, priority=5, difficulty=3, splittable=False, deadline_day=0)
    other = DynamicTask(title="Report", duration_slots=30, priority=5, difficulty=3, splittable=False, deadline_day=0)
    new = DynamicTask(title="Essay", duration_slots=20, priority=5, difficulty=3, splittable=False, deadline_day=0)
    report = propose_drops(PlanFrame(fixed, 1, [SleepRule(night=0)]), [lab, other], new, must_add=True)
    shortened = [(p, a) for p in report.proposals for a in p.actions if a.shrink]
    assert shortened
    p, a = shortened[0]
    blocks = [i for i in p.schedule if i.kind == "task" and i.title.startswith(a.title)]
    assert len(blocks) == 1 and blocks[0].end_slot - blocks[0].start_slot == a.slots_kept


def test_a_one_block_new_task_can_be_offered_shortened(conn, sid):
    essay = _tight_day(conn, sid)
    fit = build_fit_inputs(conn, sid, NOW, essay)
    report = propose_drops(fit.frame, fit.tasks, fit.new_task)
    assert any(p.new_task_added and p.new_task_slots_cut > 0 for p in report.proposals)


def test_a_multi_session_new_task_is_not_offered_shortened(conn, sid):
    _tight_day(conn, sid)
    big = ExtractedTask(title="Project", date=D.isoformat(), duration_slots=24, priority=5, difficulty=3)
    fit = build_fit_inputs(conn, sid, NOW, big)  # 6h, splittable: several sessions
    report = propose_drops(fit.frame, fit.tasks, fit.new_task)
    assert all(p.new_task_slots_cut == 0 for p in report.proposals)


def test_shortening_the_new_task_keeps_full_hours_saved_and_records_a_plan_cut(conn, sid):
    essay = _tight_day(conn, sid)
    fit = build_fit_inputs(conn, sid, NOW, essay)
    report = propose_drops(fit.frame, fit.tasks, fit.new_task)
    choice = next(p for p in report.proposals if p.new_task_slots_cut)
    summary = apply_drop_choice(conn, sid, choice, fit.planned, essay)
    saved = {t.title: t.duration_slots for _, t in EXTRACTED_TASKS.get(conn, sid)}
    assert saved["Essay"] == 16  # saved at full hours
    assert get_plan_cuts(conn, sid)[summary["new_task_id"]] == choice.new_task_slots_cut
    planned = {t.title: t.duration_slots for _, t in _planned(conn, sid, D)}
    assert planned["Essay"] == 16 - choice.new_task_slots_cut


def test_screen_and_summary_say_shorten_and_still_one_block(conn, sid):
    essay = _tight_day(conn, sid)
    shown = []
    answers = iter(["s", "1"])   
    add_task_with_fit(conn, sid, essay, NOW, ask=lambda _p: next(answers), show=shown.append)
    text = "\n".join(shown)
    assert "Shorten 'Lab' by" in text and "still one block" in text
    assert "shortened to" in text
    fit = build_fit_inputs(conn, sid, NOW, essay)
    report = propose_drops(fit.frame, fit.tasks, fit.new_task)
    lines = "\n".join(describe_proposal(1, next(p for p in report.proposals if p.new_task_slots_cut), fit.new_task))
    assert "shortened to" in lines


# ---------- session length ----------
def test_the_students_session_length_reaches_the_solver_tasks(conn, sid):
    save_settings(conn, sid, load_settings(conn, sid).model_copy(update={"default_max_session_slots": 4}))
    EXTRACTED_TASKS.add(conn, sid, ExtractedTask(title="Essay", date=D.isoformat(), duration_slots=12))
    [(_, t)] = _planned(conn, sid, D)
    assert t.max_session_slots == 4
    fit = build_fit_inputs(conn, sid, NOW, ExtractedTask(title="New", date=D.isoformat(), duration_slots=12))
    assert fit.new_task.max_session_slots == 4


def test_prompt_asks_about_splitting_using_the_students_session_length():
    ask, shown = scripted(["Quiz", "2026-10-05", "2", "", "", "n"])
    task = prompt_new_task(ask, shown.append, today=date(2026, 9, 28), session_cap=4)  # 2h > a 1h session
    assert task.splittable is False
    ask, shown = scripted(["Quiz", "2026-10-05", "2", "", ""])
    assert prompt_new_task(ask, shown.append, today=date(2026, 9, 28)).splittable is True  # default 2h cap: no ask


def test_menu_session_time_saves_and_takes_effect_immediately(conn, sid):
    ask, shown = scripted(["t", "1", "a", "Quiz", "2026-10-05", "2", "", "", "n", "q"])  # 2h > new 1h cap -> asks
    run_menu(conn, sid, ask, shown.append, today=date(2026, 9, 28))
    assert load_settings(conn, sid).default_max_session_slots == 4
    assert any("up to 1h" in l for l in shown)
    assert EXTRACTED_TASKS.get(conn, sid)[0][1].splittable is False


def test_menu_session_time_enter_keeps_and_junk_is_rejected(conn, sid):
    ask, shown = scripted(["t", "", "t", "abc", "t", "0", "q"])
    run_menu(conn, sid, ask, shown.append, today=date(2026, 9, 28))
    assert load_settings(conn, sid).default_max_session_slots == 8
    assert sum("Invalid" in l for l in shown) == 2


# ---------- review flow ----------
def _long_task():
    return ExtractionResult(tasks=[ExtractedTask(title="Report", date="2026-10-05", duration_slots=24)])


def test_review_marks_long_tasks_as_splittable_and_explains_how_to_change_it():
    ask, shown = scripted([""])
    review_extraction(_long_task(), ask, shown.append, session_cap=8)
    text = "\n".join(shown)
    assert "can be split" in text and "edit it to make it a single block" in text


def test_review_says_nothing_extra_when_no_task_is_long():
    ask, shown = scripted([""])
    review_extraction(ExtractionResult(tasks=[ExtractedTask(title="HW", date="2026-10-05")]),
                      ask, shown.append, session_cap=8)
    assert not any("split" in l for l in shown)


def test_editing_a_long_task_asks_the_split_question_and_keeps_the_answer():
    ask, shown = scripted(["1", "e", "", "", "", "", "", "n"])  # keep every field, then answer 'n' to split
    out = review_extraction(_long_task(), ask, shown.append, session_cap=8)
    assert out.tasks[0].splittable is False


def test_editing_hours_down_to_one_session_skips_the_split_question():
    ask, shown = scripted(["1", "e", "", "", "2", "", ""])  # hours -> 2h, so no split question
    out = review_extraction(_long_task(), ask, shown.append, session_cap=8)
    assert out.tasks[0].duration_slots == 8 and out.tasks[0].splittable is True


def test_review_without_a_session_length_behaves_as_before():
    ask, shown = scripted([""])
    review_extraction(_long_task(), ask, shown.append)
    assert not any("split" in l for l in shown)


def test_import_flow_uses_the_students_session_length(tmp_path, conn, sid):
    img = tmp_path / "t.png"
    img.write_bytes(b"fake-image-bytes")
    ask, shown = scripted(["y", ""])  # confirm the Groq call, then accept everything
    run_import(conn, sid, str(img), ask=ask, show=shown.append,
               extractor=lambda *a, **k: _long_task(), cache_path=str(tmp_path / "c.json"))
    assert any("can be split" in l for l in shown)


def test_describe_only_says_can_be_split_when_given_a_session_length():
    t = ExtractedTask(title="Report", date="2026-10-05", duration_slots=24)
    assert "can be split" not in describe_task(t)
    assert "can be split" in describe_task(t, 8)
    assert "one block" in describe_task(t.model_copy(update={"splittable": False}), 8)