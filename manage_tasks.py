from scheduler.db import connect, get_or_create_student
from scheduler.task_manager import run_menu

from scheduler.paths import DB_PATH  # repo root, whatever folder you run from


def main():
    conn = connect(DB_PATH)
    name = input("Student name: ").strip()
    student_id = get_or_create_student(conn, name)
    run_menu(conn, student_id)


if __name__ == "__main__":
    main()