from scheduler.schedule_extraction import extract_schedule

MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".pdf": "application/pdf",
}


def main():
    path = input("Path to your timetable image or PDF: ").strip().strip('"')

    ext = "." + path.rsplit(".", 1)[-1].lower()
    if ext not in MEDIA_TYPES:
        print(f"Unsupported file type '{ext}'. Use png, jpg, jpeg, or pdf.")
        return
    media_type = MEDIA_TYPES[ext]

    with open(path, "rb") as f:
        file_bytes = f.read()

    print("Extracting... (this calls your configured LLM_BACKEND)")
    result = extract_schedule(file_bytes, media_type)

    print("\nWeekly patterns found:")
    if not result.weekly_patterns:
        print("  (none)")
    for p in result.weekly_patterns:
        print(f"  - {p.title}: {p.day} {p.start_time}-{p.end_time}")

    print("\nDated sessions found:")
    if not result.dated_blocks:
        print("  (none)")
    for b in result.dated_blocks:
        print(f"  - {b.title}: {b.date} {b.start_time}-{b.end_time}")

    print("\nTasks found:")
    if not result.tasks:
        print("  (none)")
    for t in result.tasks:
        print(f"  - {t.title} due {t.date}")


if __name__ == "__main__":
    main()