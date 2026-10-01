from datetime import date, datetime, timedelta

import pytest

from scheduler.calendar_utils import extracted_task_to_dynamic_task
from scheduler.db import (
    add_extracted_task, connect, get_extracted_tasks, get_or_create_student, update_extracted_task,
)
from scheduler.models import ExtractedTask, ExtractionResult
from scheduler.planner import plan_from_saved
from scheduler.review import _describe
from scheduler.task_manager import prompt_new_task, run_menu

TODAY = date(2026, 9, 28)
NOW = datetime(2026, 10, 5, 8, 0)
D = date(2026, 10, 5)


def scripted(answers):
    it, shown = iter(answers), []
    return (lambda _p: next(it)), shown


@pytest.fixture
def conn():
    return connect(":memory:")


@pytest.fixture
def sid(conn):
    return get_or_create_student(conn, "Zane")


def _task(title="Essay", hours=3, splittable=True, due=D + timedelta(days=2)):
    return ExtractedTask(title=title, date=due.isoformat(), duration_slots=int(hours * 4),
                         splittable=splittable)


def test_splittable_is_hidden_from_the_llm_extraction_schema():
    assert "splittable" not in str(ExtractionResult.model_json_schema())


def test_old_saved_tasks_without_the_field_load_as_splittable():
    old = ExtractedTask.model_validate_json('{"title":"A","date":"2026-10-05","duration_slots":8}')
    assert old.splittable is True


def test_the_choice_reaches_the_solver_task():
    assert extracted_task_to_dynamic_task(_task(splittable=False), D).splittable is False
    assert extracted_task_to_dynamic_task(_task(splittable=True), D).splittable is True


def test_short_tasks_are_not_asked_about_splitting():
    ask, shown = scripted(["Quiz", "2026-10-05", "2", "", ""])  # 2h = one session, nothing to split
    task = prompt_new_task(ask, shown.append, today=TODAY)  # a further ask would raise StopIteration
    assert task.splittable is True and not any("split" in l for l in shown)


@pytest.mark.parametrize("answer, expected", [("n", False), ("y", True), ("", True)])
def test_long_tasks_ask_and_the_answer_is_kept(answer, expected):
    ask, shown = scripted(["Report", "2026-10-05", "5", "", "", answer])
    assert prompt_new_task(ask, shown.append, today=TODAY).splittable is expected


def test_unclear_answer_reasks():
    ask, shown = scripted(["Report", "2026-10-05", "5", "", "", "maybe", "n"])
    assert prompt_new_task(ask, shown.append, today=TODAY).splittable is False


def test_non_splittable_task_is_planned_as_one_block_and_splittable_as_sessions(conn, sid):
    add_extracted_task(conn, sid, _task("Whole", 3, splittable=False))
    add_extracted_task(conn, sid, _task("Parts", 3, splittable=True))
    _, _, items, _ = plan_from_saved(conn, sid, now=NOW, time_limit_seconds=10)
    whole = [i for i in items if i.title.startswith("Whole")]
    parts = [i for i in items if i.title.startswith("Parts")]
    assert len(whole) == 1 and whole[0].end_slot - whole[0].start_slot == 12
    assert len(parts) == 2


def test_menu_split_option_changes_and_saves_the_setting(conn, sid):
    add_extracted_task(conn, sid, _task("Essay", 3, splittable=True))
    ask, shown = scripted(["s", "1", "n", "q"])
    run_menu(conn, sid, ask, shown.append, today=TODAY)
    assert get_extracted_tasks(conn, sid)[0][1].splittable is False
    assert any("one block" in l for l in shown)


def test_menu_split_option_enter_keeps_the_current_setting(conn, sid):
    add_extracted_task(conn, sid, _task("Essay", 3, splittable=False))
    ask, shown = scripted(["s", "1", "", "q"])
    run_menu(conn, sid, ask, shown.append, today=TODAY)
    assert get_extracted_tasks(conn, sid)[0][1].splittable is False


def test_menu_split_option_handles_empty_cancel_and_bad_numbers(conn, sid):
    ask, shown = scripted(["s", "q"])
    run_menu(conn, sid, ask, shown.append, today=TODAY)
    assert "No tasks saved." in shown
    add_extracted_task(conn, sid, _task("Essay", 3))
    ask, shown = scripted(["s", "", "s", "9", "q"])
    run_menu(conn, sid, ask, shown.append, today=TODAY)
    assert any("between 1 and 1" in l for l in shown)
    assert get_extracted_tasks(conn, sid)[0][1].splittable is True


def test_update_only_touches_the_owners_task(conn, sid):
    tid = add_extracted_task(conn, sid, _task("Essay", 3))
    other = get_or_create_student(conn, "Other")
    assert update_extracted_task(conn, other, tid, _task("Hacked", 1)) is False
    assert get_extracted_tasks(conn, sid)[0][1].title == "Essay"


def test_list_line_marks_one_block_tasks_only():
    assert "one block" in _describe("Task", _task(splittable=False))
    assert "one block" not in _describe("Task", _task(splittable=True))