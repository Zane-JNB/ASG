from scheduler.preferences import pending_approvals, resolve_pending 
from scheduler.settings_menu import describe_pending 
from scheduler.db import connect, get_or_create_student
from scheduler.llm_backends import is_backend_failure
from scheduler.reflection_cycle import get_proposals, apply_and_log, reflect_and_record

from scheduler.paths import DB_PATH  # repo root, whatever folder you run from

_STATUS = {                                                    
    "learned_update_applied": "Settings updated.",
    "approval_needed": "Enough evidence -- this needs your approval.",
    "evidence_recorded": "Evidence recorded -- no settings changed yet.",
    "threshold_at_limit": "Enough evidence, but the setting is already at its limit -- no change.",
    "proposal_ignored": "Nothing changed.",
    "no_proposals": "No changes proposed.",
}

def main():
    confirm = input("Groq (free tier, but a real API call). Continue? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return

    conn = connect(DB_PATH)
    name = input("Student name: ").strip()
    student_id = get_or_create_student(conn, name)

    reflection_text = input("How did it go? ").strip()
    while True:  # a backend failure (rate limit, retired model, no key...) must not lose the text
        try:
            outcome, summary = reflect_and_record(conn, student_id, reflection_text)
            break
        except Exception as e:
            if not is_backend_failure(e):
                raise  # a real bug: keep the traceback
            print(f"Could not process your reflection: {e}")
            if input("Try again? Your text is kept. [y/N] ").strip().lower() != "y":
                print(f"Nothing was saved. Your reflection was:\n{reflection_text}")
                return

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