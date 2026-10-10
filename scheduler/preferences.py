import sqlite3
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from enum import Enum
from pydantic import ValidationError
from scheduler.db import (clear_evidence, load_approval_mode, load_evidence_times, load_settings, load_tiers,
                          log_reflection, save_approval_mode, save_settings, set_tier, load_evidence, save_evidence,
                          transaction)
from scheduler.models import ProfileSettings
from scheduler.reflection import ReflectionResult, PreferenceChangeProposal, apply_proposal
from scheduler.preference_policy import POLICY, FieldPolicy, Tier

OUTCOME_USER_EDIT = "user_edit"            
OUTCOME_INTERNAL_EDIT = "internal_edit"    
OUTCOME_OWNERSHIP_CHANGE = "ownership_change"  
EVIDENCE_THRESHOLD = 3  
_MAGNITUDE_ORDER = {"small": 0, "medium": 1, "large": 2}  
APPROVAL_AUTO, APPROVAL_ASK = "auto", "ask"                                                 
OUTCOME_PENDING = "approval_needed"; OUTCOME_APPROVED = "approved_update_applied"           
OUTCOME_DECLINED = "proposal_declined"; OUTCOME_APPROVAL_MODE = "approval_mode_change"      
OUTCOME_APPLIED = "learned_update_applied"; OUTCOME_EVIDENCE = "evidence_recorded"  
OUTCOME_AT_LIMIT = "threshold_at_limit"; OUTCOME_IGNORED = "proposal_ignored"; OUTCOME_NONE = "no_proposals"
_OUTCOME_PRIORITY = [OUTCOME_APPLIED, OUTCOME_PENDING, OUTCOME_EVIDENCE, OUTCOME_AT_LIMIT, OUTCOME_IGNORED]  
EVIDENCE_TTL_DAYS = 14 


class Actor(str, Enum):  
    USER = "user"
    MODEL = "model"
    INTERNAL = "internal"   # developer/app code

class PreferenceError(ValueError):  
    pass

def get_effective(conn, student_id) -> ProfileSettings:  #   (the solver keeps using this plain object)
    return load_settings(conn, student_id)

def get_ownership(conn, student_id) -> dict[str, Tier]:  
    return load_tiers(conn, student_id)

def _policy(field):  
    p = POLICY.get(field)
    if p is None:
        raise PreferenceError(f"'{field}' is not a known setting.")
    return p

def _check_may_edit(field, tiers, actor):  
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

def _validated(before, changes):  
    try:
        after = ProfileSettings(**{**before.model_dump(), **changes})
        after.default_sleep_rule(0)   # reuses SleepRule's own min<=target, earliest<=pref<=latest
    except ValidationError as e:
        raise PreferenceError("Invalid value: " + "; ".join(x["msg"] for x in e.errors())) from e
    return after

def set_values(conn, student_id, changes: dict, actor: Actor) -> ProfileSettings:  
    tiers = load_tiers(conn, student_id)
    for field in changes:
        _check_may_edit(field, tiers, actor)          # all-or-nothing
    before = load_settings(conn, student_id)
    after = _validated(before, changes)
    if after == before:
        return before                                 # no-op: no write, no log row
    summary = ", ".join(f"{f}: {getattr(before, f)} -> {getattr(after, f)}" for f in changes)
    outcome = OUTCOME_USER_EDIT if actor == Actor.USER else OUTCOME_INTERNAL_EDIT
    with transaction(conn):                      # settings + audit row, one transaction
        save_settings(conn, student_id, after)
        log_reflection(conn, student_id, f"edit by {actor.value}: {summary}", before=before,
                       after=after, applied=True, outcome=outcome)
    return after

def user_edit(conn, student_id, field, value):  
    return set_values(conn, student_id, {field: value}, Actor.USER)

def change_tier(conn, student_id, field, new_tier: Tier, actor: Actor) -> bool:  
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
    with transaction(conn):
        set_tier(conn, student_id, field, new_tier)
        clear_evidence(conn, student_id, field)   # stale either way
        log_reflection(conn, student_id, f"ownership: {field} {current.value} -> {new_tier.value}",
                       before=settings, after=settings, applied=False,
                       outcome=OUTCOME_OWNERSHIP_CHANGE)
    return True


@dataclass(frozen=True)  
class FieldResult:
    field: str
    status: str     # an OUTCOME_* string
    message: str    # friendly, CLI-ready

@dataclass(frozen=True)  
class ReflectionOutcome:
    outcome: str
    results: list[FieldResult]
    settings: ProfileSettings

def next_evidence(prev, direction, magnitude):  #   (a dial: +-1 per reflection)
    vote = 1 if direction == "increase" else -1
    if prev is None or prev[0] == 0:
        return vote, magnitude                      # first vote
    if (prev[0] > 0) != (vote > 0):
        return prev[0] + vote, prev[1]              #   opposing vote cancels ONE vote; 0 = fully cancelled
    return prev[0] + vote, min(prev[1], magnitude, key=_MAGNITUDE_ORDER.__getitem__)

def collapse_proposals(proposals):  #  one vote per field per reflection
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

def learnable_fields(conn, student_id) -> list[str]: 
    tiers = load_tiers(conn, student_id)
    return [n for n, p in POLICY.items() if p.model_learnable and tiers[n] == Tier.MODEL_LEARNED]

def _threshold_change(settings, field, score, magnitude):  
    proposal = PreferenceChangeProposal(field=field, magnitude=magnitude,
        direction="increase" if score > 0 else "decrease",
        reason=f"consistent evidence across {EVIDENCE_THRESHOLD} reflections")
    old, new = getattr(settings, field), getattr(apply_proposal(settings, proposal), field)
    if new == old:
        raise PreferenceError("already at its limit")
    return _validated(settings, {field: new}), old, new

def process_reflection(conn, student_id, reflection_text, result, now = None) -> ReflectionOutcome:
    now = now or datetime.now(timezone.utc)                                                         
    tiers, evidence = load_tiers(conn, student_id), live_evidence(conn, student_id, now)
    mode = load_approval_mode(conn, student_id)
    before = settings = load_settings(conn, student_id)
    results = [];
    writes = [(f, None) for f in set(load_evidence(conn, student_id)) - set(evidence)]

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
                writes.append((field, (score, smallest) if score else None))    
                word = "increase" if score > 0 else "decrease"
                results.append(FieldResult(field, OUTCOME_EVIDENCE,
                    f"Noted: opposing suggestions for '{label}' cancelled out. Starting fresh." if score == 0 else
                    f"Noted: '{label}' may need to {word} ({abs(score)}/{EVIDENCE_THRESHOLD} reflections). No change yet."))
                continue
            try:
                candidate, old, new = _threshold_change(settings, field, score, smallest)   
            except PreferenceError:
                writes.append((field, None))         # can't move: threshold consumed
                results.append(FieldResult(field, OUTCOME_AT_LIMIT,
                    f"'{label}' reached the threshold but can't move further within its limits."))
            else:
                if mode == APPROVAL_ASK:             #   evidence held AT the threshold = "pending"
                    held = (max(-EVIDENCE_THRESHOLD, min(EVIDENCE_THRESHOLD, score)), smallest)
                    writes.append((field, held))
                    results.append(FieldResult(field, OUTCOME_PENDING,
                        f"'{label}' has enough evidence to change -- waiting for your approval."))
                else:
                    writes.append((field, None))     # threshold consumed
                    settings = candidate
                    results.append(FieldResult(field, OUTCOME_APPLIED,
                        f"'{label}' changed {old} -> {new} after repeated reflections."))

    outcome = next((o for o in _OUTCOME_PRIORITY if any(r.status == o for r in results)), OUTCOME_NONE)
    with transaction(conn):                      # evidence + settings + log: one transaction
        for field, state in writes:
            if state is None: clear_evidence(conn, student_id, field)
            else: save_evidence(conn, student_id, field, *state, at=now.isoformat())
        if settings != before:
            save_settings(conn, student_id, settings)
        log_reflection(conn, student_id, reflection_text, before=before, after=settings,
                       applied=settings != before, outcome=outcome)
    return ReflectionOutcome(outcome, results, settings)

def get_approval_mode(conn, student_id) -> str:  
    return load_approval_mode(conn, student_id)

def set_approval_mode(conn, student_id, mode, actor) -> bool:  
    if actor == Actor.MODEL:
        raise PreferenceError("The learning system can't change approval settings.")
    if mode not in (APPROVAL_AUTO, APPROVAL_ASK):
        raise PreferenceError(f"Unknown approval mode '{mode}'.")
    current = load_approval_mode(conn, student_id)
    if mode == current:
        return False
    settings = load_settings(conn, student_id)
    with transaction(conn):
        save_approval_mode(conn, student_id, mode)
        log_reflection(conn, student_id, f"approval mode: {current} -> {mode}", before=settings,
                       after=settings, applied=False, outcome=OUTCOME_APPROVAL_MODE)
    return True

@dataclass(frozen=True)  
class PendingChange:
    field: str
    label: str
    direction: str
    old: int
    new: int

def pending_approvals(conn, student_id, now=None) -> list[PendingChange]:
    if load_approval_mode(conn, student_id) != APPROVAL_ASK:
        return []
    now = now or datetime.now(timezone.utc)                                              
    tiers, settings, out = load_tiers(conn, student_id), load_settings(conn, student_id), []
    for field, (score, magnitude) in live_evidence(conn, student_id, now).items():      
        policy = POLICY.get(field)
        if (abs(score) < EVIDENCE_THRESHOLD or policy is None or not policy.model_learnable
                or tiers.get(field) != Tier.MODEL_LEARNED):
            continue
        try:
            _, old, new = _threshold_change(settings, field, score, magnitude)
        except PreferenceError:
            continue
        out.append(PendingChange(field, policy.label, "increase" if score > 0 else "decrease", old, new))
    return out

def resolve_pending(conn, student_id, field, approve: bool, now=None) -> FieldResult:  
    pending = next((p for p in pending_approvals(conn, student_id, now) if p.field == field), None)
    if pending is None:
        raise PreferenceError("Nothing is waiting for your approval for that setting.")
    before = load_settings(conn, student_id)
    after = _validated(before, {field: pending.new}) if approve else before
    outcome = OUTCOME_APPROVED if approve else OUTCOME_DECLINED
    with transaction(conn):
        clear_evidence(conn, student_id, field)   # consumed either way: 1 approval = 1 change
        if approve:
            save_settings(conn, student_id, after)
        log_reflection(conn, student_id,
                       f"{'approved' if approve else 'declined'}: {field} {pending.old} -> {pending.new}",
                       before=before, after=after, applied=approve, outcome=outcome)
    msg = (f"'{pending.label}' changed {pending.old} -> {pending.new}." if approve else
           f"OK -- '{pending.label}' left as it is. Evidence starts fresh.")
    return FieldResult(field, outcome, msg)

def _utc(dt: datetime) -> datetime:  #  naive datetimes are read as UTC, so naive and aware values can be compared
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

def live_evidence(conn, student_id, now):  #  stored evidence minus anything idle for >= TTL days
    times, limit = load_evidence_times(conn, student_id), timedelta(days=EVIDENCE_TTL_DAYS)
    return {f: v for f, v in load_evidence(conn, student_id).items()
            if not times.get(f) or _utc(now) - _utc(datetime.fromisoformat(times[f])) < limit}