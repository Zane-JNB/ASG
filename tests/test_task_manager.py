from datetime import date

from scheduler.db import connect, get_or_create_student, get_extracted_tasks, replace_extraction
from scheduler.models import ExtractedTask, ExtractionResult, WeeklyPattern
from scheduler.planner import plan_from_saved
from scheduler.task_manager import prompt_new_task, run_menu

TODAY = date(2026, 9, 28)

def scripted(answers):
    it = iter(answers)
    shown = []
    return (lambda _prompt: next(it)), shown

def _conn():
    conn = connect(":memory:")
    return conn, get_or_create_student(conn, "Z")

def _saved(conn, sid):
    return [t for _, t in get_extracted_tasks(conn, sid)]

def test_add_with_all_defaults_takes_title_and_date_only():
    ask, shown = scripted(["Essay", "2026-10-05", "", "", ""])
    task = prompt_new_task(ask, shown.append, today=TODAY)
    assert task == ExtractedTask(title="Essay", date="2026-10-05")  # 1h, priority 3, difficulty 3

def test_add_with_custom_values():
    ask, shown = scripted(["Report", "2026-10-05", "6", "5", "4", "n"])
    task = prompt_new_task(ask, shown.append, today=TODAY)
    assert (task.duration_slots, task.priority, task.difficulty) == (24, 5, 4)

def test_bad_input_reasks_only_that_field():
    ask, shown = scripted(["", "Essay",                       # blank title -> re-asked
                           "2026-13-45", "2026-09-27", "2026-10-05",  # impossible date, today, then valid
                           "0", "2",                          # under 15 minutes, then 2h
                           "9", "5",                          # priority out of range, then 5
                           "", ])                             # difficulty Enter = default
    task = prompt_new_task(ask, shown.append, today=TODAY)
    assert (task.title, task.date, task.duration_slots, task.priority, task.difficulty) == \
           ("Essay", "2026-10-05", 8, 5, 3)
    text = " ".join(" ".join(shown).split()) 
    assert "required" in text and "today or later" in text and "1 to 5" in text

def test_menu_add_list_and_quit():
    conn, sid = _conn()
    ask, shown = scripted(["a", "Essay", "2026-10-05", "", "", "", "l", "q"])
    run_menu(conn, sid, ask, shown.append, today=TODAY)
    assert [t.title for t in _saved(conn, sid)] == ["Essay"]
    assert "Added." in shown and any(s.startswith("1. Essay due 2026-10-05") for s in shown)

def test_list_is_sorted_by_due_date_and_delete_uses_those_numbers():
    conn, sid = _conn()
    for title, due in (("Late", "2026-11-01"), ("Soon", "2026-10-01")):
        replace_extraction(conn, sid, ExtractionResult(tasks=[ExtractedTask(title=title, date=due)]))
    ask, shown = scripted(["d", "1", "q"])  # #1 in the sorted list is "Soon"
    run_menu(conn, sid, ask, shown.append, today=TODAY)
    assert [t.title for t in _saved(conn, sid)] == ["Late"]

def test_delete_all_needs_the_word_yes():
    conn, sid = _conn()
    replace_extraction(conn, sid, ExtractionResult(
        tasks=[ExtractedTask(title="A", date="2026-10-01"), ExtractedTask(title="B", date="2026-10-02")]))
    ask, shown = scripted(["x", "y", "x", "", "q"])  # 'y' and Enter must NOT delete
    run_menu(conn, sid, ask, shown.append, today=TODAY)
    assert len(_saved(conn, sid)) == 2 and shown.count("Cancelled -- nothing deleted.") == 2
    ask, shown = scripted(["x", "yes", "q"])
    run_menu(conn, sid, ask, shown.append, today=TODAY)
    assert _saved(conn, sid) == [] and "Deleted 2 task(s)." in shown

def test_delete_all_only_touches_tasks_not_classes():
    conn, sid = _conn()
    replace_extraction(conn, sid, ExtractionResult(
        weekly_patterns=[WeeklyPattern(title="DS", day="Mon", start_time="09:00", end_time="11:00")],
        tasks=[ExtractedTask(title="A", date="2026-10-01")]))
    ask, shown = scripted(["x", "yes", "q"])
    run_menu(conn, sid, ask, shown.append, today=TODAY)
    plan_from_saved(conn, sid, 3, start_date=TODAY, time_limit_seconds=5)  # classes still there: no error

def test_tasks_are_per_student():
    conn, sid = _conn()
    other = get_or_create_student(conn, "Other")
    ask, shown = scripted(["a", "Mine", "2026-10-05", "", "", "", "q"])
    run_menu(conn, sid, ask, shown.append, today=TODAY)
    assert _saved(conn, other) == []

def test_menu_handles_junk_and_empty_states():
    conn, sid = _conn()
    ask, shown = scripted(["zzz", "l", "d", "x", "q"])
    run_menu(conn, sid, ask, shown.append, today=TODAY)
    assert "Choose a, l, d, f, c, s, t, r, m, x or q." in shown and shown.count("No tasks saved.") == 3

def test_added_task_reaches_the_plan():
    conn, sid = _conn()
    ask, shown = scripted(["a", "Essay", "2026-10-02", "2", "", "", "q"])
    run_menu(conn, sid, ask, shown.append, today=TODAY)
    _, _, items, _ = plan_from_saved(conn, sid, 7, start_date=date(2026, 9, 29), time_limit_seconds=10)
    essay = [i for i in items if i.title.startswith("Essay")]
    assert essay and all(i.day <= 3 for i in essay)  # due Fri 2 Oct = day 3 from Tue 29 Sep

def test_due_today_is_accepted():   
    ask, shown = scripted(["Quiz", "2026-09-28", "", "", ""])
    task = prompt_new_task(ask, shown.append, today=TODAY)
    assert task.date == "2026-09-28"

def test_menu_m_opens_the_commute_menu():   
    conn, sid = _conn()
    ask, shown = scripted(["m", "l", "b", "q"])
    run_menu(conn, sid, ask, shown.append, today=TODAY)
    assert "No commutes saved." in shown