"""Who may change which setting (tiers), and how reflections earn a change: evidence across
separate reflections, a threshold, then an automatic change or the student's approval."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

import annotated_types
from pydantic import ValidationError

from scheduler.db import (clear_evidence, load_approval_mode, load_evidence, load_settings,
                          load_tiers, log_reflection, save_approval_mode, save_evidence, save_settings, set_tier,
                          transaction)
from scheduler.models import ProfileSettings
from scheduler.preference_policy import MAGNITUDES, POLICY, ApprovalMode, Direction, FieldPolicy, Magnitude, Tier
from scheduler.reflection import PreferenceChangeProposal

EVIDENCE_THRESHOLD = 3
EVIDENCE_TTL_DAYS = 14
Evidence = tuple[int, Magnitude]  # (net votes, the smallest magnitude voted)


class Outcome(StrEnum):
    """What one logged change or reflection did. The values are stored in reflections.outcome."""
    USER_EDIT = "user_edit"
    INTERNAL_EDIT = "internal_edit"
    OWNERSHIP_CHANGE = "ownership_change"
    PENDING = "approval_needed"
    APPROVED = "approved_update_applied"
    DECLINED = "proposal_declined"
    APPROVAL_MODE = "approval_mode_change"
    APPLIED = "learned_update_applied"
    EVIDENCE = "evidence_recorded"
    AT_LIMIT = "threshold_at_limit"
    IGNORED = "proposal_ignored"
    NONE = "no_proposals"


# a reflection's overall outcome is the first of these that any field reached
_OUTCOME_PRIORITY = [Outcome.APPLIED, Outcome.PENDING, Outcome.EVIDENCE, Outcome.AT_LIMIT, Outcome.IGNORED]


class Actor(StrEnum):
    USER = "user"
    MODEL = "model"
    INTERNAL = "internal"  # developer/app code


class PreferenceError(Exception):
    """A settings change our own rules refuse (tiers, limits), with a message for the student.
    Not a ValueError, so entry points never mistake it for a bad answer from the LLM."""


def _policy(field: str) -> FieldPolicy:
    p = POLICY.get(field)
    if p is None:
        raise PreferenceError(f"'{field}' is not a known setting.")
    return p


def _is_learnable(field: str, tiers: dict[str, Tier]) -> bool:
    """Reflections may move this field: the policy allows it and the student left it automatic."""
    p = POLICY.get(field)
    return p is not None and p.model_learnable and tiers.get(field) == Tier.MODEL_LEARNED


def check_may_edit(field: str, tiers: dict[str, Tier], actor: Actor) -> None:
    """Raise PreferenceError (with a message for the student) unless actor may set field directly."""
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


def _bounds(field: str) -> tuple[int, int | None]:
    """(lowest, highest or None) that ProfileSettings' own Field(...) constraints allow."""
    lo, hi = 0, None
    for c in ProfileSettings.model_fields[field].metadata:
        if isinstance(c, annotated_types.Gt):
            lo = c.gt + 1
        elif isinstance(c, annotated_types.Ge):
            lo = c.ge
        elif isinstance(c, annotated_types.Lt):
            hi = c.lt - 1
        elif isinstance(c, annotated_types.Le):
            hi = c.le
    return lo, hi


def stepped_value(settings: ProfileSettings, field: str, direction: Direction, magnitude: Magnitude) -> int:
    """The field moved one POLICY step of that magnitude, clamped to its valid range. The LLM
    only ever names the direction and magnitude; this is where they become a number."""
    delta = POLICY[field].deltas[magnitude]
    lo, hi = _bounds(field)
    value = max(getattr(settings, field) + (delta if direction == "increase" else -delta), lo)
    return value if hi is None else min(value, hi)


def _direction(score: int) -> Direction:
    return "increase" if score > 0 else "decrease"


def validated_settings(before: ProfileSettings, changes: dict) -> ProfileSettings:
    """before with changes applied, checked like a saved profile (incl. its sleep rule)."""
    try:
        after = ProfileSettings(**{**before.model_dump(), **changes})
        after.default_sleep_rule(0)  # reuses SleepRule's own min<=target, earliest<=pref<=latest
    except ValidationError as e:
        raise PreferenceError("Invalid value: " + "; ".join(x["msg"] for x in e.errors())) from e
    return after


def set_values(conn, student_id, changes: dict, actor: Actor) -> ProfileSettings:
    """Set several fields at once, all or nothing, and log the edit. Returns the saved settings."""
    tiers = load_tiers(conn, student_id)
    for field in changes:
        check_may_edit(field, tiers, actor)
    before = load_settings(conn, student_id)
    after = validated_settings(before, changes)
    if after == before:
        return before  # no-op: no write, no log row
    summary = ", ".join(f"{f}: {getattr(before, f)} -> {getattr(after, f)}" for f in changes)
    outcome = Outcome.USER_EDIT if actor == Actor.USER else Outcome.INTERNAL_EDIT
    with transaction(conn):  # settings + audit row, one transaction
        save_settings(conn, student_id, after)
        log_reflection(conn, student_id, f"edit by {actor.value}: {summary}", before=before,
                       after=after, applied=True, outcome=outcome)
    return after


def change_tier(conn, student_id, field, new_tier: Tier, actor: Actor) -> bool:
    """Switch who owns field. False if it already had that tier. The value is never touched."""
    policy = _policy(field)
    new_tier = Tier(new_tier)
    if actor == Actor.MODEL:
        raise PreferenceError("The learning system can't change who owns a setting.")
    current = load_tiers(conn, student_id)[field]
    if new_tier == current:
        return False  # no-op keeps pending evidence
    if actor == Actor.USER:
        if not policy.user_claimable:
            raise PreferenceError(f"'{policy.label}' can't be switched between manual and automatic.")
        if {current, new_tier} != {Tier.USER, Tier.MODEL_LEARNED}:
            raise PreferenceError("You can only switch a setting between manual and automatic.")
    if new_tier == Tier.USER and not policy.user_editable:
        raise PreferenceError(f"'{policy.label}' can't be user-owned.")
    if new_tier == Tier.MODEL_LEARNED and not policy.model_learnable:
        raise PreferenceError(f"'{policy.label}' can't be learned automatically.")
    settings = load_settings(conn, student_id)
    with transaction(conn):
        set_tier(conn, student_id, field, new_tier)
        clear_evidence(conn, student_id, field)  # stale either way
        log_reflection(conn, student_id, f"ownership: {field} {current.value} -> {new_tier.value}",
                       before=settings, after=settings, applied=False, outcome=Outcome.OWNERSHIP_CHANGE)
    return True


@dataclass(frozen=True)
class FieldResult:
    field: str
    status: Outcome
    message: str  # friendly, CLI-ready


@dataclass(frozen=True)
class ReflectionOutcome:
    outcome: Outcome
    results: list[FieldResult]
    settings: ProfileSettings


def next_evidence(prev: Evidence | None, direction: Direction, magnitude: Magnitude) -> Evidence:
    """A dial: +-1 per reflection. An opposing vote cancels one vote; 0 = fully cancelled."""
    vote = 1 if direction == "increase" else -1
    if prev is None or prev[0] == 0:
        return vote, magnitude  # first vote
    if (prev[0] > 0) != (vote > 0):
        return prev[0] + vote, prev[1]
    return prev[0] + vote, min(prev[1], magnitude, key=MAGNITUDES.index)


def collapse_proposals(proposals: list[PreferenceChangeProposal]) -> dict[str, tuple[Direction, Magnitude] | None]:
    """One vote per field per reflection: {field: (direction, smallest magnitude)}, or None for a
    field that got both directions."""
    grouped = {}
    for p in proposals:
        grouped.setdefault(p.field, []).append(p)
    votes = {}
    for field, group in grouped.items():
        if len({p.direction for p in group}) > 1:
            votes[field] = None  # self-contradicting -> skipped
        else:
            votes[field] = (group[0].direction,
                            min((p.magnitude for p in group), key=MAGNITUDES.index))
    return votes


def learnable_fields(conn, student_id) -> list[str]:
    tiers = load_tiers(conn, student_id)
    return [n for n in POLICY if _is_learnable(n, tiers)]


def _threshold_change(settings: ProfileSettings, field: str, score: int,
                      magnitude: Magnitude) -> tuple[ProfileSettings, int, int]:
    """(settings with the field moved one step, old value, new value) once evidence is in."""
    old, new = getattr(settings, field), stepped_value(settings, field, _direction(score), magnitude)
    if new == old:
        raise PreferenceError("already at its limit")
    return validated_settings(settings, {field: new}), old, new


def _why_ignored(field: str, vote, tiers: dict[str, Tier]) -> str | None:
    """Why a field's vote can't count as evidence, or None if it can."""
    policy = POLICY.get(field)
    label = policy.label if policy else field
    if policy is None or not policy.model_learnable:
        return f"'{label}' isn't a tunable setting."
    if tiers[field] != Tier.MODEL_LEARNED:  # authorization re-checked here, always
        who = "protected" if tiers[field] == Tier.LOCKED else "set by you"
        return f"'{label}' is {who}, so it wasn't changed."
    if vote is None:
        return f"'{label}' got contradictory suggestions in one reflection; skipped."
    return None


def process_reflection(conn, student_id, reflection_text, result, now=None) -> ReflectionOutcome:
    """Count one reflection's proposals as evidence. A field that reaches the threshold changes
    (auto mode) or waits for approval (ask mode). Evidence, settings and the log row are saved
    in one transaction."""
    now = now or datetime.now(timezone.utc)
    tiers, evidence = load_tiers(conn, student_id), live_evidence(conn, student_id, now)
    mode = load_approval_mode(conn, student_id)
    before = settings = load_settings(conn, student_id)
    results = []
    writes = [(f, None) for f in set(load_evidence(conn, student_id)) - set(evidence)]  # expired

    for field, vote in collapse_proposals(result.proposals).items():
        reason = _why_ignored(field, vote, tiers)
        if reason:
            results.append(FieldResult(field, Outcome.IGNORED, reason))
            continue
        label = POLICY[field].label
        score, smallest = next_evidence(evidence.get(field), *vote)
        if abs(score) < EVIDENCE_THRESHOLD:
            writes.append((field, (score, smallest) if score else None))
            results.append(FieldResult(field, Outcome.EVIDENCE,
                f"Noted: opposing suggestions for '{label}' cancelled out. Starting fresh." if score == 0 else
                f"Noted: '{label}' may need to {_direction(score)} ({abs(score)}/{EVIDENCE_THRESHOLD} reflections). No change yet."))
            continue
        try:
            candidate, old, new = _threshold_change(settings, field, score, smallest)
        except PreferenceError:
            writes.append((field, None))  # can't move: threshold consumed
            results.append(FieldResult(field, Outcome.AT_LIMIT,
                f"'{label}' reached the threshold but can't move further within its limits."))
            continue
        if mode == ApprovalMode.ASK:  # evidence held AT the threshold = "pending"
            writes.append((field, (max(-EVIDENCE_THRESHOLD, min(EVIDENCE_THRESHOLD, score)), smallest)))
            results.append(FieldResult(field, Outcome.PENDING,
                f"'{label}' has enough evidence to change -- waiting for your approval."))
        else:
            writes.append((field, None))  # threshold consumed
            settings = candidate
            results.append(FieldResult(field, Outcome.APPLIED,
                f"'{label}' changed {old} -> {new} after repeated reflections."))

    outcome = next((o for o in _OUTCOME_PRIORITY if any(r.status == o for r in results)), Outcome.NONE)
    with transaction(conn):  # evidence + settings + log: one transaction
        for field, state in writes:
            if state is None:
                clear_evidence(conn, student_id, field)
            else:
                save_evidence(conn, student_id, field, *state, at=now.isoformat())
        if settings != before:
            save_settings(conn, student_id, settings)
        log_reflection(conn, student_id, reflection_text, before=before, after=settings,
                       applied=settings != before, outcome=outcome)
    return ReflectionOutcome(outcome, results, settings)


def set_approval_mode(conn, student_id, mode: ApprovalMode | str, actor: Actor) -> bool:
    """Switch between auto and ask. False if it was already that mode."""
    if actor == Actor.MODEL:
        raise PreferenceError("The learning system can't change approval settings.")
    try:
        mode = ApprovalMode(mode)
    except ValueError:
        raise PreferenceError(f"Unknown approval mode '{mode}'.") from None
    current = load_approval_mode(conn, student_id)
    if mode == current:
        return False
    settings = load_settings(conn, student_id)
    with transaction(conn):
        save_approval_mode(conn, student_id, mode)
        log_reflection(conn, student_id, f"approval mode: {current} -> {mode}", before=settings,
                       after=settings, applied=False, outcome=Outcome.APPROVAL_MODE)
    return True


@dataclass(frozen=True)
class PendingChange:
    field: str
    label: str
    direction: Direction
    old: int
    new: int


def pending_approvals(conn, student_id, now=None) -> list[PendingChange]:
    """Changes that reached the threshold in ask mode and wait for the student's yes or no."""
    if load_approval_mode(conn, student_id) != ApprovalMode.ASK:
        return []
    now = now or datetime.now(timezone.utc)
    tiers, settings, out = load_tiers(conn, student_id), load_settings(conn, student_id), []
    for field, (score, magnitude) in live_evidence(conn, student_id, now).items():
        if abs(score) < EVIDENCE_THRESHOLD or not _is_learnable(field, tiers):
            continue
        try:
            _, old, new = _threshold_change(settings, field, score, magnitude)
        except PreferenceError:
            continue
        out.append(PendingChange(field, POLICY[field].label, _direction(score), old, new))
    return out


def resolve_pending(conn, student_id, field, approve: bool, now=None) -> FieldResult:
    pending = next((p for p in pending_approvals(conn, student_id, now) if p.field == field), None)
    if pending is None:
        raise PreferenceError("Nothing is waiting for your approval for that setting.")
    before = load_settings(conn, student_id)
    after = validated_settings(before, {field: pending.new}) if approve else before
    outcome = Outcome.APPROVED if approve else Outcome.DECLINED
    with transaction(conn):
        clear_evidence(conn, student_id, field)  # consumed either way: 1 approval = 1 change
        if approve:
            save_settings(conn, student_id, after)
        log_reflection(conn, student_id,
                       f"{'approved' if approve else 'declined'}: {field} {pending.old} -> {pending.new}",
                       before=before, after=after, applied=approve, outcome=outcome)
    msg = (f"'{pending.label}' changed {pending.old} -> {pending.new}." if approve else
           f"OK -- '{pending.label}' left as it is. Evidence starts fresh.")
    return FieldResult(field, outcome, msg)


def live_evidence(conn, student_id: int, now: datetime) -> dict[str, Evidence]:
    """Stored evidence minus anything idle for EVIDENCE_TTL_DAYS or longer."""
    return load_evidence(conn, student_id, fresh_since=now - timedelta(days=EVIDENCE_TTL_DAYS))
