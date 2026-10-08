from scheduler.db import connect, get_or_create_student
from scheduler.import_flow import run_import
from scheduler.paths import CACHE_PATH, DB_PATH  # repo root, whatever folder you run from


def main():
    conn = connect(DB_PATH)
    name = input("Student name: ").strip()
    student_id = get_or_create_student(conn, name)
    path = input("Path to timetable image/PDF (or a saved .json to replay free, e.g. the last\n"
                 f"extraction: {CACHE_PATH}): ").strip().strip('"')
    run_import(conn, student_id, path)


if __name__ == "__main__":
    main()