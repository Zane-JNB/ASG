from scheduler.models import DynamicTask, FixedBlock, SLOTS_PER_DAY, time_to_slot, slot_to_time
from scheduler.solver import plan_day_cp


def sleep(day):
    return [
        FixedBlock(title="Sleep", start_slot=time_to_slot("00:00"), end_slot=time_to_slot("07:00"), day=day),
        FixedBlock(title="Sleep", start_slot=time_to_slot("23:00"), end_slot=time_to_slot("24:00"), day=day),
    ]


fixed_blocks = [
    *sleep(0),
    *sleep(1),
    FixedBlock(title="Data Structures lecture", start_slot=time_to_slot("09:00"), end_slot=time_to_slot("11:00"), day=0),
    FixedBlock(title="Football practice", start_slot=time_to_slot("16:00"), end_slot=time_to_slot("18:00"), day=0),
    FixedBlock(title="Data Structures lecture", start_slot=time_to_slot("09:00"), end_slot=time_to_slot("11:00"), day=1),
]

tasks = [
    DynamicTask(title="Study for Stats test", duration_slots=12, priority=5, difficulty=4,
                deadline_day=1, deadline_slot=time_to_slot("09:00")),
    DynamicTask(title="Algorithms report", duration_slots=8, priority=4, difficulty=3,
                deadline_day=0, deadline_slot=time_to_slot("22:00")),
    DynamicTask(title="Lab write-up", duration_slots=6, priority=3, difficulty=2),
    DynamicTask(title="Read chapter 5", duration_slots=4, priority=2, difficulty=2),
    DynamicTask(title="Problem set", duration_slots=16, priority=3, difficulty=3),
]

items, unscheduled = plan_day_cp(fixed_blocks, tasks, num_days=2)

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