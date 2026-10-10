"""The reflection flow: the LLM proposes, preferences decide. apply_and_log is the demo's
legacy path (the student's y/N is the consent, so no evidence threshold)."""
import sqlite3
from datetime import datetime

from scheduler.db import load_settings, log_reflection, save_settings, transaction
from scheduler.preferences import (PreferenceError, ReflectionOutcome, learnable_fields, process_reflection,
                                   stepped_value, validated_settings)
from scheduler.reflection import ReflectionResult, propose_preference_changes


def apply_and_log(conn: sqlite3.Connection, student_id: int, reflection_text: str,
                  result: ReflectionResult, accepted: list[bool]):
    """Apply only the accepted proposals, save the new settings, and log the whole
    reflection (before/after snapshot + an overall applied flag).

    accepted must have one bool per result.proposals, in the same order (the caller's
    y/N answers). Rejected proposals are dropped silently -- only chosen ones are
    applied. Returns the settings actually saved (== before, unchanged, if nothing
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
    for p in (p for p, ok in zip(result.proposals, accepted) if ok and p.field in allowed):
        try:
            after = validated_settings(after, {p.field: stepped_value(after, p.field, p.direction, p.magnitude)})
        except PreferenceError:
            continue  # this one would make the settings invalid: skip it, keep the rest

    applied = after != before
    with transaction(conn):  # settings + audit row, one transaction
        if applied:
            save_settings(conn, student_id, after)
        log_reflection(conn, student_id, reflection_text, before=before, after=after, applied=applied)

    return after

def reflect_and_record(conn: sqlite3.Connection, student_id: int, reflection_text: str, *, now: datetime,
                       client=None) -> tuple[ReflectionOutcome, str]:
    """One reflection end to end: ask the LLM for proposals about the fields it may move, then
    count them as evidence. Returns (ReflectionOutcome, the LLM's summary)."""
    allowed = learnable_fields(conn, student_id)  # prompt filter only; process_reflection re-checks
    result = propose_preference_changes(reflection_text, client=client, allowed_fields=allowed)
    return process_reflection(conn, student_id, reflection_text, result, now=now), result.summary
