import sqlite3
from dataclasses import dataclass
from enum import Enum
from pydantic import ValidationError
from scheduler.db import (clear_evidence, load_settings, load_tiers,
                          log_reflection, save_settings, set_tier, load_evidence, save_evidence)
from scheduler.models import ProfileSettings
from scheduler.reflection import ReflectionResult, PreferenceChangeProposal, apply_proposal
from scheduler.preference_policy import POLICY, FieldPolicy, Tier

OUTCOME_USER_EDIT = "user_edit"            # NEW
OUTCOME_INTERNAL_EDIT = "internal_edit"    # NEW
OUTCOME_OWNERSHIP_CHANGE = "ownership_change"  # NEW
EVIDENCE_THRESHOLD = 3  # NEW
_MAGNITUDE_ORDER = {"small": 0, "medium": 1, "large": 2}  # NEW
OUTCOME_APPLIED = "learned_update_applied"; OUTCOME_EVIDENCE = "evidence_recorded"  # NEW
OUTCOME_AT_LIMIT = "threshold_at_limit"; OUTCOME_IGNORED = "proposal_ignored"; OUTCOME_NONE = "no_proposals"
_OUTCOME_PRIORITY = [OUTCOME_APPLIED, OUTCOME_EVIDENCE, OUTCOME_AT_LIMIT, OUTCOME_IGNORED]


class Actor(str, Enum):  # NEW
    USER = "user"
    MODEL = "model"
    INTERNAL = "internal"   # developer/app code

class PreferenceError(ValueError):  # NEW: str(e) is a friendly, CLI-ready message
    pass

def get_effective(conn, student_id) -> ProfileSettings:  # NEW (the solver keeps using this plain object)
    return load_settings(conn, student_id)

def get_ownership(conn, student_id) -> dict[str, Tier]:  # NEW
    return load_tiers(conn, student_id)

def _policy(field):  # NEW
    p = POLICY.get(field)
    if p is None:
        raise PreferenceError(f"'{field}' is not a known setting.")
    return p

def _check_may_edit(field, tiers, actor):  # NEW
    policy = _policy(field)
    if actor == Actor.MODEL:
        raise PreferenceError("The learning system can't edit settings directly.")
    if actor == Actor.INTERNAL:
        return
    tier = tiers[field]
    if tier == Tier.LOCKED:
        raise PreferenceError(f"'{policy.label}' is protected and can't be changed.")
    if not policy.user_editable:
        raise PreferenceError(f"'{policy.label}' is an internal tuning value and can't be edited.")
    if tier != Tier.USER:
        raise PreferenceError(f"'{policy.label}' is currently managed automatically. "
                              "Take ownership of it first if you want to set it yourself.")

def _validated(before, changes):  # NEW
    try:
        after = ProfileSettings(**{**before.model_dump(), **changes})
        after.default_sleep_rule(0)   # reuses SleepRule's own min<=target, earliest<=pref<=latest
    except ValidationError as e:
        raise PreferenceError("Invalid value: " + "; ".join(x["msg"] for x in e.errors())) from e
    return after

def set_values(conn, student_id, changes: dict, actor: Actor) -> ProfileSettings:  # NEW
    tiers = load_tiers(conn, student_id)
    for field in changes:
        _check_may_edit(field, tiers, actor)          # all-or-nothing
    before = load_settings(conn, student_id)
    after = _validated(before, changes)
    if after == before:
        return before                                 # no-op: no write, no log row
    summary = ", ".join(f"{f}: {getattr(before, f)} -> {getattr(after, f)}" for f in changes)
    outcome = OUTCOME_USER_EDIT if actor == Actor.USER else OUTCOME_INTERNAL_EDIT
    try:
        save_settings(conn, student_id, after, commit=False)
        log_reflection(conn, student_id, f"edit by {actor.value}: {summary}", before=before,
                       after=after, applied=True, outcome=outcome, commit=False)
        conn.commit()                                 # settings + audit row, one transaction
    except Exception:
        conn.rollback(); raise
    return after

def user_edit(conn, student_id, field, value):  # NEW
    return set_values(conn, student_id, {field: value}, Actor.USER)

def change_tier(conn, student_id, field, new_tier: Tier, actor: Actor) -> bool:  # NEW
    policy = _policy(field)
    new_tier = Tier(new_tier)
    if actor == Actor.MODEL:
        raise PreferenceError("The learning system can't change who owns a setting.")
    current = load_tiers(conn, student_id)[field]
    if new_tier == current:
        return False                                  # no-op keeps pending evidence
    if actor == Actor.USER:
        if not policy.user_claimable:
            raise PreferenceError(f"'{policy.label}' can't be switched between manual and automatic.")
        if {current, new_tier} != {Tier.USER, Tier.MODEL_LEARNED}:
            raise PreferenceError("You can only switch a setting between manual and automatic.")
    if new_tier == Tier.USER and not policy.user_editable:
        raise PreferenceError(f"'{policy.label}' can't be user-owned.")
    if new_tier == Tier.MODEL_LEARNED and not policy.model_learnable:
        raise PreferenceError(f"'{policy.label}' can't be learned automatically.")
    settings = load_settings(conn, student_id)        # value is never touched
    try:
        set_tier(conn, student_id, field, new_tier, commit=False)
        clear_evidence(conn, student_id, field, commit=False)   # stale either way
        log_reflection(conn, student_id, f"ownership: {field} {current.value} -> {new_tier.value}",
                       before=settings, after=settings, applied=False,
                       outcome=OUTCOME_OWNERSHIP_CHANGE, commit=False)
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return True


@dataclass(frozen=True)  # NEW
class FieldResult:
    field: str
    status: str     # an OUTCOME_* string
    message: str    # friendly, CLI-ready

@dataclass(frozen=True)  # NEW
class ReflectionOutcome:
    outcome: str
    results: list[FieldResult]
    settings: ProfileSettings

def next_evidence(prev, direction, magnitude):  # NEW (pure scoring step)
    vote = 1 if direction == "increase" else -1
    if prev is None or prev[0] == 0 or (prev[0] > 0) != (vote > 0):
        return vote, magnitude                      # first vote, or conflict: restart the streak
    return prev[0] + vote, min(prev[1], magnitude, key=_MAGNITUDE_ORDER.__getitem__)

def collapse_proposals(proposals):  # NEW: one vote per field per reflection
    grouped = {}
    for p in proposals:
        grouped.setdefault(p.field, []).append(p)
    votes = {}
    for field, group in grouped.items():
        if len({p.direction for p in group}) > 1:
            votes[field] = None                     # self-contradicting -> skipped
        else:
            votes[field] = (group[0].direction,
                            min((p.magnitude for p in group), key=_MAGNITUDE_ORDER.__getitem__))
    return votes

def learnable_fields(conn, student_id) -> list[str]:  # NEW (feeds the prompt filter)
    tiers = load_tiers(conn, student_id)
    return [n for n, p in POLICY.items() if p.model_learnable and tiers[n] == Tier.MODEL_LEARNED]

def process_reflection(conn, student_id, reflection_text, result) -> ReflectionOutcome:  # NEW
    tiers, evidence = load_tiers(conn, student_id), load_evidence(conn, student_id)
    before = settings = load_settings(conn, student_id)
    results, writes = [], []                         # writes: (field, new_evidence | None=clear)

    for field, vote in collapse_proposals(result.proposals).items():
        policy = POLICY.get(field)
        label = policy.label if policy else field
        if policy is None or not policy.model_learnable:
            results.append(FieldResult(field, OUTCOME_IGNORED, f"'{label}' isn't a tunable setting."))
        elif tiers[field] != Tier.MODEL_LEARNED:     # authorization re-checked here, always
            who = "protected" if tiers[field] == Tier.LOCKED else "set by you"
            results.append(FieldResult(field, OUTCOME_IGNORED, f"'{label}' is {who}, so it wasn't changed."))
        elif vote is None:
            results.append(FieldResult(field, OUTCOME_IGNORED,
                f"'{label}' got contradictory suggestions in one reflection; skipped."))
        else:
            score, smallest = next_evidence(evidence.get(field), *vote)
            if abs(score) < EVIDENCE_THRESHOLD:
                writes.append((field, (score, smallest)))
                word = "increase" if score > 0 else "decrease"
                results.append(FieldResult(field, OUTCOME_EVIDENCE,
                    f"Noted: '{label}' may need to {word} ({abs(score)}/{EVIDENCE_THRESHOLD} reflections). No change yet."))
                continue
            writes.append((field, None))             # threshold consumed either way
            proposal = PreferenceChangeProposal(field=field, magnitude=smallest,
                direction="increase" if score > 0 else "decrease",
                reason=f"consistent evidence across {EVIDENCE_THRESHOLD} reflections")
            old, new = getattr(settings, field), getattr(apply_proposal(settings, proposal), field)
            try:
                if new == old:
                    raise PreferenceError("already at its limit")
                settings = _validated(settings, {field: new})   # incl. sleep-window consistency
                results.append(FieldResult(field, OUTCOME_APPLIED,
                    f"'{label}' changed {old} -> {new} after repeated reflections."))
            except PreferenceError:
                results.append(FieldResult(field, OUTCOME_AT_LIMIT,
                    f"'{label}' reached the threshold but can't move further within its limits."))

    outcome = next((o for o in _OUTCOME_PRIORITY if any(r.status == o for r in results)), OUTCOME_NONE)
    try:                                             # evidence + settings + log: one transaction
        for field, state in writes:
            if state is None: clear_evidence(conn, student_id, field, commit=False)
            else: save_evidence(conn, student_id, field, *state, commit=False)
        if settings != before:
            save_settings(conn, student_id, settings, commit=False)
        log_reflection(conn, student_id, reflection_text, before=before, after=settings,
                       applied=settings != before, outcome=outcome, commit=False)
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return ReflectionOutcome(outcome, results, settings)