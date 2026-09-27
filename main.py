"""Reference/demo script -- not a real app yet, just shows the pieces working together:
build a schedule, simulate a reflection, apply the resulting preference changes, and
re-solve to see what actually changed. Run with: python3 main.py
"""
from scheduler.db import connect, get_or_create_student, load_settings
from scheduler.models import (
    Exam, FixedBlock, SleepRule, StudyPlanRule, slot_to_time, time_to_slot,
)
from scheduler.reflection_cycle import apply_and_log, get_proposals, rerun_schedule
from scheduler.solver import build_schedule, generate_study_tasks, sleep_warnings, task_warnings

DB_PATH = "scheduler.db"
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


def main():
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

    # --- simulate a reflection (fake backend -- free, offline, no API key needed) ---
    reflection_text = (
        "This week felt really rushed -- no breaks between class, work, and studying."
    )
    print(f"\n=== Reflection ===\n  \"{reflection_text}\"")
    result = get_proposals(reflection_text)
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


if __name__ == "__main__":
    main()