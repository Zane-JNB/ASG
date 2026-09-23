from scheduler.models import DynamicTask, FixedBlock, ProfileSettings, Exam, SLOTS_PER_DAY, time_to_slot, slot_to_time
from scheduler.solver import build_schedule, generate_study_tasks, sleep_warnings, task_warnings


num_days = 2
settings = ProfileSettings()  # defaults for now -- override any field to tune this student's plan

fixed_blocks = [
    FixedBlock(title="Data Structures lecture", start_slot=time_to_slot("09:00"), end_slot=time_to_slot("11:00"), day=0),
    FixedBlock(title="Football practice", start_slot=time_to_slot("16:00"), end_slot=time_to_slot("18:00"), day=0),
    FixedBlock(title="Data Structures lecture", start_slot=time_to_slot("09:00"), end_slot=time_to_slot("11:00"), day=1),
]

sleep_rules = [settings.default_sleep_rule(night=d) for d in range(num_days)]

exams = [
    Exam(title="Stats test", day=1, slot=time_to_slot("09:00"), difficulty=4),
]
study_tasks = generate_study_tasks(exams, settings=settings)

other_tasks = [
    DynamicTask(title="Algorithms report", duration_slots=8, priority=4, difficulty=3,
                deadline_day=0, deadline_slot=time_to_slot("22:00")),
    DynamicTask(title="Lab write-up", duration_slots=6, priority=3, difficulty=2),
    DynamicTask(title="Read chapter 5", duration_slots=4, priority=2, difficulty=2),
    DynamicTask(title="Problem set", duration_slots=16, priority=3, difficulty=3),
]

tasks = study_tasks + other_tasks

items, unscheduled = build_schedule(fixed_blocks, tasks, num_days=num_days, sleep_rules=sleep_rules,
                                 settings=settings)

for item in items:
    end = (
        f"{slot_to_time(item.end_slot - SLOTS_PER_DAY)} (+1 day)"
        if item.end_slot > SLOTS_PER_DAY
        else slot_to_time(item.end_slot)
    )
    print(f"Day {item.day + 1}  {slot_to_time(item.start_slot)}-{end}  [{item.kind}] {item.title}")

if unscheduled:
    print("\nCould not fit:")
    for task in unscheduled:
        print(f"  - {task.title}")

warnings = sleep_warnings(sleep_rules, items) + task_warnings(unscheduled)
if warnings:
    print("\nWarnings:")
    for w in warnings:
        print(f"  [{w.severity}] {w.message}")