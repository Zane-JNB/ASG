from datetime import datetime   

from scheduler.completion import record_plan   
from scheduler.db import connect, get_or_create_student
from scheduler.planner import plan_from_saved, format_plan

DB_PATH = "scheduler.db"


def main():
    conn = connect(DB_PATH)
    name = input("Student name: ").strip()
    student_id = get_or_create_student(conn, name)
    now = datetime.now()   
    try:
        anchor, fixed, items, warnings = plan_from_saved(conn, student_id, now=now)
    except ValueError as e:
        print(e)
        return
    print("\n".join(format_plan(anchor, fixed, items, warnings)))
    record_plan(conn, student_id, anchor, items, now)  #  check-ins know which sessions have passed


if __name__ == "__main__":
    main()