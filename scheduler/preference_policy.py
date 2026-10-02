from dataclasses import dataclass
from enum import Enum
from scheduler.models import ProfileSettings

class Tier(str, Enum):  # NEW
    LOCKED = "locked"
    USER = "user"
    MODEL_LEARNED = "model_learned"

@dataclass(frozen=True)  # NEW
class FieldPolicy:
    default_tier: Tier
    label: str
    description: str
    model_learnable: bool = False
    user_editable: bool = False
    user_claimable: bool = False
    deltas: dict[str, int] | None = None

    def __post_init__(self):  # NEW: contradictory policies can't be constructed
        if self.model_learnable != (self.deltas is not None):
            raise ValueError("deltas must be set if and only if model_learnable")
        if self.user_claimable and not (self.model_learnable and self.user_editable):
            raise ValueError("a claimable field must be both model_learnable and user_editable")
        if self.default_tier == Tier.LOCKED and (
            self.model_learnable or self.user_editable or self.user_claimable):
            raise ValueError("a locked field cannot be learnable, editable or claimable")
        if self.default_tier == Tier.USER and not self.user_editable:
            raise ValueError("a user-tier field must be user_editable")
        if self.default_tier == Tier.MODEL_LEARNED and not self.model_learnable:
            raise ValueError("a model_learned-tier field must be model_learnable")

def _locked(label, description):  # NEW
    return FieldPolicy(Tier.LOCKED, label, description)

def _user_only(label, description):  # NEW  (student edits; model never learns; can't change tier)
    return FieldPolicy(Tier.USER, label, description, user_editable=True)

def _internal(label, description, deltas):  # NEW  (model learns; student can't edit/claim)
    return FieldPolicy(Tier.MODEL_LEARNED, label, description, model_learnable=True, deltas=deltas)

def _claimable(label, description, deltas, default_tier=Tier.MODEL_LEARNED):  # NEW
    return FieldPolicy(default_tier, label, description, model_learnable=True,
                       user_editable=True, user_claimable=True, deltas=deltas)

S, M, L = "small", "medium", "large"  # NEW
POLICY: dict[str, FieldPolicy] = {  # NEW
    "buffer_slots": _claimable("Break time", "Gap after tasks/classes (15-min slots).", {S: 1, M: 2, L: 4}),
    "default_max_session_slots": _claimable("Longest study session", "Session cap (15-min slots).", {S: 2, M: 4, L: 8}, Tier.USER),
    "default_sleep_length_slots": _claimable("Sleep target", "Sleep you aim for (15-min slots).", {S: 2, M: 4, L: 8}),
    "default_preferred_bed": _claimable("Preferred bedtime", "Slot index on the 2-day timeline.", {S: 2, M: 4, L: 8}),
    "default_sleep_min_slots": _user_only("Minimum sleep", "Sleep floor (15-min slots)."),
    "default_earliest_bed": _user_only("Earliest bedtime", "Slot index on the 2-day timeline."),
    "default_latest_bed": _user_only("Latest bedtime", "Slot index on the 2-day timeline."),
    "reminders_enabled": _user_only("Reminders on/off", "Master reminder switch."),
    "reminder_min_difficulty": _user_only("Reminder min difficulty", "1-5, or none."),
    "reminder_min_priority": _user_only("Reminder min priority", "1-5, or none."),
    "bedtime_penalty": _internal("Bedtime drift weight", "Cost per slot off preferred bedtime.", {S: 10, M: 25, L: 50}),
    "same_day_penalty": _internal("Same-day spread weight", "Cost of doubling up a task in a day.", {S: 500, M: 1000, L: 2000}),
    "sleep_target_penalty": _internal("Sleep target weight", "Cost per slot under sleep target.", {S: 500, M: 1000, L: 2500}),
    "presence_bonus": _locked("Task fit bonus", "Protects fitting tasks over shuffling."),
    "sleep_min_penalty": _locked("Min-sleep weight", "Protects minimum sleep."),
    "drop_priority_weight": _locked("Drop priority weight", "Dropping cost per priority point."),
    "drop_difficulty_weight": _locked("Drop difficulty weight", "Dropping cost per difficulty."),
    "drop_deadline_multiplier": _locked("Deadline drop multiplier", "Protects deadlines."),
    "drop_sleep_weight": _locked("Drop sleep weight", "Dropping cost per slot of lost sleep."),
    "drop_hard_flag_penalty": _locked("Drop hard-flag penalty", "Cost per hard warning."),
    "plan_horizon_max_days": _locked("Plan horizon", "Maximum planning days."),
    "shrink_steps": _locked("Shrink steps", "Fractions tried when fitting a task."),
}

MODEL_DELTAS = {n: dict(p.deltas) for n, p in POLICY.items() if p.model_learnable}  # NEW