import sqlite3

from scheduler.db import load_settings, save_settings, log_reflection
from scheduler.reflection import ReflectionResult, apply_proposal, propose_preference_changes
from scheduler.solver import build_schedule
from scheduler.models import DynamicTask, FixedBlock, SleepRule
from scheduler.preferences import PreferenceError, ReflectionOutcome, _validated, learnable_fields, process_reflection


def apply_and_log(conn: sqlite3.Connection, student_id: int, reflection_text: str,
                  result: ReflectionResult, accepted: list[bool]):
    """Apply only the accepted proposals, save the new settings, and log the whole
    reflection (before/after snapshot + an overall applied flag).

    accepted must have one bool per result.proposals, in the same order (the caller's
    y/N answers). Rejected proposals are dropped silently -- only chosen ones reach
    apply_all. Returns the settings actually saved (== before, unchanged, if nothing
    was accepted or every accepted proposal net out to a no-op).

    Legacy direct path (the demo's y/N prompt is the student's consent, so no evidence
    threshold). It still honours ownership and validity: proposals for fields the student
    owns (not learnable) or whose result is invalid (e.g. sleep below its minimum) are skipped.
    """
    if len(accepted) != len(result.proposals):
        raise ValueError(
            f"accepted has {len(accepted)} entries but there are {len(result.proposals)} proposals"
        )

    before = load_settings(conn, student_id)
    allowed = learnable_fields(conn, student_id)
    after = before
    for proposal, ok in zip(result.proposals, accepted):
        if not ok or proposal.field not in allowed:
            continue
        value = getattr(apply_proposal(after, proposal), proposal.field)
        try:
            after = _validated(after, {proposal.field: value})
        except PreferenceError:
            continue

    applied = after != before
    if applied:
        save_settings(conn, student_id, after)
    log_reflection(conn, student_id, reflection_text, before=before, after=after, applied=applied)

    return after

def rerun_schedule(conn, student_id, fixed_blocks, tasks, sleep_rules=None, num_days=1,
                   time_limit_seconds=30.0):
    """Re-solve a plan against this student's CURRENT settings -- e.g. right after a
    reflection changed them. Thin wrapper over build_schedule: the only thing it adds is
    loading settings from the db instead of trusting the caller to pass the right ones.

    This does NOT persist fixed_blocks/tasks/sleep_rules anywhere -- the db has no table
    for a student's schedule inputs yet, so the caller still supplies the plan each time.
    """
    settings = load_settings(conn, student_id)
    return build_schedule(
        fixed_blocks, tasks, num_days=num_days, sleep_rules=sleep_rules,
        time_limit_seconds=time_limit_seconds, settings=settings,
    )

def get_proposals(reflection_text, client=None, allowed_fields=None) -> ReflectionResult:   
    return propose_preference_changes(reflection_text, client=client, allowed_fields=allowed_fields)  

def reflect_and_record(conn, student_id, reflection_text, client=None):  
    allowed = learnable_fields(conn, student_id)       # prompt filter only; process_reflection re-checks
    result = get_proposals(reflection_text, client=client, allowed_fields=allowed)
    return process_reflection(conn, student_id, reflection_text, result), result.summary