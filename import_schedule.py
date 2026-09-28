from scheduler.db import connect, get_or_create_student
from scheduler.import_flow import run_import

DB_PATH = "scheduler.db"


def main():
    conn = connect(DB_PATH)
    name = input("Student name: ").strip()
    student_id = get_or_create_student(conn, name)
    path = input("Path to timetable image/PDF (or a saved .json to replay free): ").strip().strip('"')
    run_import(conn, student_id, path)


if __name__ == "__main__":
    main()