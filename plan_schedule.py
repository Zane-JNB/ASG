from datetime import datetime

from scheduler.completion import record_plan
from scheduler.db import connect, get_or_create_student
from scheduler.paths import DB_PATH  # repo root, whatever folder you run from
from scheduler.planner import format_plan, plan_from_saved


def main():
    conn = connect(DB_PATH)
    name = input("Student name: ").strip()
    student_id = get_or_create_student(conn, name)
    now = datetime.now()
    try:
        plan = plan_from_saved(conn, student_id, now=now)
    except (ValueError, RuntimeError) as e:  # bad saved data, or the solver found nothing in time
        print(e)
        return
    print("\n".join(format_plan(plan)))
    record_plan(conn, student_id, plan.anchor, plan.items, now)  # so check-ins know which sessions have passed


if __name__ == "__main__":
    main()