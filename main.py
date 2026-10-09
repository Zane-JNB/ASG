"""Reference/demo script -- not a real app yet, just shows the pieces working together:
build a schedule, simulate a reflection, apply the resulting preference changes, and
re-solve to see what actually changed. Run with: python3 main.py
"""

from datetime import date, datetime
from scheduler.add_with_fit import add_task_with_fit
from scheduler.db import add_dated_block, add_extracted_task, connect, get_or_create_student, get_plan_cuts, load_settings
from scheduler.llm_backends import is_backend_failure
from scheduler.models import (
    DatedBlock, Exam, ExtractedTask, FixedBlock, SleepRule, StudyPlanRule, slot_to_time, time_to_slot,
)
from scheduler.reflection_cycle import apply_and_log, get_proposals, rerun_schedule
from scheduler.solver import build_schedule, generate_study_tasks, sleep_warnings, task_warnings
from scheduler.planner import format_plan, plan_from_saved

DB_PATH = ":memory:"
NUM_DAYS = 5  # today through the exam


def print_schedule(items):
    for item in items:
        start = slot_to_time(item.start_slot)
        end = slot_to_time(item.end_slot % (24 * 4))  # wrap midnight-crossing back to a clock time
        print(f"  Day {item.day}  {start}-{end}  [{item.kind}]  {item.title}")


def print_warnings(warnings):
    if not warnings:
        print("  (none)")
        return
    for w in warnings:
        print(f"  [{w.severity}] {w.kind}: {w.message}")


def demo_reflection():
    conn = connect(DB_PATH)
    student_id = get_or_create_student(conn, "Zane")

    fixed_blocks = [
        FixedBlock(title="Software Engineering Lecture",
                  start_slot=time_to_slot("09:00"), end_slot=time_to_slot("10:30"), day=0),
        FixedBlock(title="Part-time job",
                  start_slot=time_to_slot("14:00"), end_slot=time_to_slot("18:00"), day=0),
        FixedBlock(title="Systems Analysis Lecture",
                  start_slot=time_to_slot("09:00"), end_slot=time_to_slot("10:30"), day=1),
        FixedBlock(title="Part-time job",
                  start_slot=time_to_slot("14:00"), end_slot=time_to_slot("18:00"), day=2),
    ]

    exams = [
        Exam(title="Systems Analysis Midterm", day=4, slot=time_to_slot("09:00"),
             difficulty=4, priority=4),
    ]
    settings = load_settings(conn, student_id)
    tasks = generate_study_tasks(exams, rule=StudyPlanRule(), settings=settings)

    sleep_rules = [settings.default_sleep_rule(night=n) for n in range(NUM_DAYS)]

    print(f"=== Settings before ===\n  buffer_slots={settings.buffer_slots}")

    print("\n=== Initial schedule ===")
    items, unscheduled = build_schedule(
        fixed_blocks, tasks, num_days=NUM_DAYS, sleep_rules=sleep_rules, settings=settings,
    )
    print_schedule(items)
    print("\n--- Warnings ---")
    print_warnings(sleep_warnings(sleep_rules, items) + task_warnings(unscheduled))

    # --- simulate a reflection (one real Groq call; needs GROQ_API_KEY) ---
    reflection_text = (
        "This week felt really rushed -- no breaks between class, work, and studying."
    )
    print(f"\n=== Reflection ===\n  \"{reflection_text}\"")
    try:
        result = get_proposals(reflection_text)
    except Exception as e:
        if not is_backend_failure(e):
            raise  # a real bug: keep the traceback
        print(f"  Reflection skipped (Groq call failed): {e}")
        return
    print(f"  {result.summary}")
    for p in result.proposals:
        print(f"  - {p.field}: {p.direction} ({p.magnitude}) -- {p.reason}")

    accepted = [True] * len(result.proposals)  # demo auto-accepts everything proposed
    new_settings = apply_and_log(conn, student_id, reflection_text, result, accepted)
    print(f"\n=== Settings after ===\n  buffer_slots={new_settings.buffer_slots}")

    print("\n=== Re-solved schedule ===")
    items2, unscheduled2 = rerun_schedule(
        conn, student_id, fixed_blocks, tasks, sleep_rules=sleep_rules, num_days=NUM_DAYS,
    )
    print_schedule(items2)
    print("\n--- Warnings ---")
    print_warnings(sleep_warnings(sleep_rules, items2) + task_warnings(unscheduled2))

def demo_make_room():  
    d, now = date(2026, 10, 5), datetime(2026, 10, 5, 9, 0)
    scripts = (("MANUAL", ["m", "1", "d", "s"]),
               ("SEMI-AUTOMATIC", ["s", "1"]),
               ("AUTOMATIC", ["a", "y"]))
    for label, answers in scripts:
        conn = connect(":memory:")
        sid = get_or_create_student(conn, "Demo")
        add_dated_block(conn, sid, DatedBlock(title="Class", date=d.isoformat(), start_time="09:00", end_time="17:00"))
        add_extracted_task(conn, sid, ExtractedTask(title="Lab", date=d.isoformat(), duration_slots=20, priority=4, difficulty=3))
        essay = ExtractedTask(title="Essay", date=d.isoformat(), duration_slots=16, priority=5, difficulty=3)
        it = iter(answers)

        def ask(prompt):  # echo the scripted answer so the output reads like a real session
            answer = next(it)
            print(f"{prompt}{answer}")
            return answer

        print(f"\n=============== {label} ===============")
        add_task_with_fit(conn, sid, essay, now, ask=ask, show=print)
        print(f"Plan cuts saved: {get_plan_cuts(conn, sid)}")
        anchor, fixed, items, warnings = plan_from_saved(conn, sid, now=now, time_limit_seconds=10)
        print("\n".join(format_plan(anchor, fixed, items, warnings)))


def main():  
    demo_make_room()
    print("\n\n=============== REFLECTION (one Groq call) ===============")
    demo_reflection()


if __name__ == "__main__":
    main()