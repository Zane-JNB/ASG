import os

from scheduler.db import connect, get_or_create_student
from scheduler.reflection_cycle import get_proposals, apply_and_log

DB_PATH = "scheduler.db"


def main():
    backend = os.environ.get("LLM_BACKEND", "fake")
    if backend != "fake":
        cost_note = "paid" if backend == "anthropic" else "free-tier but a real API call"
        confirm = input(
            f"LLM_BACKEND={backend} ({cost_note}). Continue? [y/N] "
        ).strip().lower()
        if confirm != "y":
            print("Aborted.")
            return

    conn = connect(DB_PATH)
    name = input("Student name: ").strip()
    student_id = get_or_create_student(conn, name)

    reflection_text = input("How did it go? ").strip()
    result = get_proposals(reflection_text)

    print(f"\n{result.summary}\n")
    if not result.proposals:
        apply_and_log(conn, student_id, reflection_text, result, accepted=[])
        print("No changes proposed.")
        return

    accepted = []
    for p in result.proposals:
        print(f"- {p.field}: {p.direction} ({p.magnitude}) -- {p.reason}")
        answer = input("  Apply this change? [y/N] ").strip().lower()
        accepted.append(answer == "y")

    apply_and_log(conn, student_id, reflection_text, result, accepted)
    print(f"\nApplied {sum(accepted)}/{len(accepted)} change(s). Settings saved.")


if __name__ == "__main__":
    main()