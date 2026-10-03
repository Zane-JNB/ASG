import os

from scheduler.preferences import pending_approvals, resolve_pending 
from scheduler.settings_menu import describe_pending 
from scheduler.db import connect, get_or_create_student
from scheduler.reflection_cycle import get_proposals, apply_and_log, reflect_and_record

DB_PATH = "scheduler.db"

_STATUS = {                                                    
    "learned_update_applied": "Settings updated.",
    "approval_needed": "Enough evidence -- this needs your approval.",
    "evidence_recorded": "Evidence recorded -- no settings changed yet.",
    "threshold_at_limit": "Enough evidence, but the setting is already at its limit -- no change.",
    "proposal_ignored": "Nothing changed.",
    "no_proposals": "No changes proposed.",
}

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
    outcome, summary = reflect_and_record(conn, student_id, reflection_text)   

    if summary:
        print(f"\n{summary}\n")
    for r in outcome.results:                                                   
        print(f"- {r.message}")
    print(_STATUS[outcome.outcome])                                             

    for p in pending_approvals(conn, student_id):                         
        answer = input(f"  Apply this change? {describe_pending(p)} [y/N] ").strip().lower()
        print(f"- {resolve_pending(conn, student_id, p.field, answer == 'y').message}")

if __name__ == "__main__":
    main()