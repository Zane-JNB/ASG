from scheduler.db import connect, get_or_create_student
from scheduler.task_manager import run_menu

DB_PATH = "scheduler.db"


def main():
    conn = connect(DB_PATH)
    name = input("Student name: ").strip()
    student_id = get_or_create_student(conn, name)
    run_menu(conn, student_id)


if __name__ == "__main__":
    main()