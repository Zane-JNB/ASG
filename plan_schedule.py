from datetime import datetime

from scheduler.completion import record_plan
from scheduler.db import connect, get_or_create_student
from scheduler.planner import plan_from_saved, format_plan

from scheduler.paths import DB_PATH  # repo root, whatever folder you run from


def main():
    conn = connect(DB_PATH)
    name = input("Student name: ").strip()
    student_id = get_or_create_student(conn, name)
    now = datetime.now()
    try:
        anchor, fixed, items, warnings = plan_from_saved(conn, student_id, now=now)
    except (ValueError, RuntimeError) as e:  # bad saved data, or the solver found nothing in time
        print(e)
        return
    print("\n".join(format_plan(anchor, fixed, items, warnings)))
    # so check-ins know which sessions have passed
    record_plan(conn, student_id, anchor, items, now)


if __name__ == "__main__":
    main()