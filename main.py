from scheduler.greedy import plan_day
from scheduler.solver import plan_day_cp
from scheduler.models import DynamicTask, FixedBlock, time_to_slot, slot_to_time

fixed_blocks = [
    FixedBlock(title="Sleep", start_slot=time_to_slot("00:00"), end_slot=time_to_slot("07:00")),
    FixedBlock(title="Data Structures lecture", start_slot=time_to_slot("09:00"), end_slot=time_to_slot("11:00")),
    FixedBlock(title="Football practice", start_slot=time_to_slot("16:00"), end_slot=time_to_slot("18:00")),
    FixedBlock(title="Sleep", start_slot=time_to_slot("23:00"), end_slot=time_to_slot("24:00")),
]

tasks = [
    DynamicTask(title="Algorithms report", duration_slots=8, priority=4, difficulty=3),
    DynamicTask(title="Study for Stats test", duration_slots=12, priority=5, difficulty=4),
    DynamicTask(title="Read chapter 5", duration_slots=4, priority=2, difficulty=2),
    DynamicTask(title="Lab write-up", duration_slots=6, priority=3, difficulty=2),
]

def show(name, items, unscheduled):
    print(f"--- {name} ---")
    for item in items:
        print(f"{slot_to_time(item.start_slot)}-{slot_to_time(item.end_slot)}  [{item.kind}] {item.title}")
    for task in unscheduled:
        print(f"  could not fit: {task.title}")
    print()

show("greedy", *plan_day(fixed_blocks, tasks))
show("solver", *plan_day_cp(fixed_blocks, tasks))