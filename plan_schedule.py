from scheduler.db import connect, get_or_create_student
from scheduler.planner import plan_from_saved, format_plan

DB_PATH = "scheduler.db"


def main():
    conn = connect(DB_PATH)
    name = input("Student name: ").strip()
    student_id = get_or_create_student(conn, name)
    days = input("Days to plan [7]: ").strip()
    try:
        anchor, fixed, items, warnings = plan_from_saved(conn, student_id, int(days) if days else 7)
    except ValueError as e:
        print(e)
        return
    print("\n".join(format_plan(anchor, fixed, items, warnings)))


if __name__ == "__main__":
    main()