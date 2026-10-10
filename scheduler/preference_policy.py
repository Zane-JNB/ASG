"""Who may change each ProfileSettings field, and by how much a reflection may move it."""
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Literal, get_args

Magnitude = Literal["small", "medium", "large"]
MAGNITUDES: tuple[Magnitude, ...] = get_args(Magnitude)  # smallest first
Direction = Literal["increase", "decrease"]


class Tier(StrEnum):
    """Who owns a field now. The values are stored in preference_tiers.tier."""
    LOCKED = "locked"
    USER = "user"
    MODEL_LEARNED = "model_learned"


class ApprovalMode(StrEnum):
    """How learned changes are approved. The values are stored in preference_settings."""
    AUTO = "auto"  # applied once the evidence threshold is reached
    ASK = "ask"  # wait for the student's yes or no


class Kind(Enum):
    """What a field can ever be: who may edit it, and whether reflections may learn it."""
    LOCKED = "locked"  # nobody
    USER_ONLY = "user_only"  # the student only
    INTERNAL = "internal"  # reflections only
    CLAIMABLE = "claimable"  # reflections, until the student claims it (and back)


@dataclass(frozen=True)
class FieldPolicy:
    kind: Kind
    label: str
    description: str
    deltas: dict[Magnitude, int] | None = None  # learnable kinds only: one step per magnitude
    claimed: bool = False  # claimable only: starts as the student's

    def __post_init__(self):
        if self.model_learnable != (self.deltas is not None):
            raise ValueError("deltas must be set if and only if the field is learnable")
        if self.claimed and self.kind is not Kind.CLAIMABLE:
            raise ValueError("only a claimable field can start claimed")

    @property
    def model_learnable(self) -> bool:
        return self.kind in (Kind.INTERNAL, Kind.CLAIMABLE)

    @property
    def user_editable(self) -> bool:
        return self.kind in (Kind.USER_ONLY, Kind.CLAIMABLE)

    @property
    def user_claimable(self) -> bool:
        return self.kind is Kind.CLAIMABLE

    @property
    def default_tier(self) -> Tier:
        if self.kind is Kind.LOCKED:
            return Tier.LOCKED
        if self.kind is Kind.USER_ONLY or self.claimed:
            return Tier.USER
        return Tier.MODEL_LEARNED


LOCKED, USER_ONLY, INTERNAL, CLAIMABLE = Kind
S, M, L = MAGNITUDES
POLICY: dict[str, FieldPolicy] = {
    "buffer_slots": FieldPolicy(CLAIMABLE, "Break time", "Gap after tasks/classes (15-min slots).", {S: 1, M: 2, L: 4}),
    "default_max_session_slots": FieldPolicy(CLAIMABLE, "Longest study session", "Session cap (15-min slots).", {S: 2, M: 4, L: 8}, claimed=True),
    "default_sleep_length_slots": FieldPolicy(CLAIMABLE, "Sleep target", "Sleep you aim for (15-min slots).", {S: 2, M: 4, L: 8}),
    "default_preferred_bed": FieldPolicy(CLAIMABLE, "Preferred bedtime", "Slot index on the 2-day timeline.", {S: 2, M: 4, L: 8}),
    "default_sleep_min_slots": FieldPolicy(USER_ONLY, "Minimum sleep", "Sleep floor (15-min slots)."),
    "default_earliest_bed": FieldPolicy(USER_ONLY, "Earliest bedtime", "Slot index on the 2-day timeline."),
    "default_latest_bed": FieldPolicy(USER_ONLY, "Latest bedtime", "Slot index on the 2-day timeline."),
    "wake_buffer_slots": FieldPolicy(USER_ONLY, "Wake-up buffer", "Time to get ready before the first class or commute (15-min slots)."),
    "reminders_enabled": FieldPolicy(USER_ONLY, "Reminders on/off", "Master reminder switch."),
    "reminder_min_difficulty": FieldPolicy(USER_ONLY, "Reminder min difficulty", "1-5, or none."),
    "reminder_min_priority": FieldPolicy(USER_ONLY, "Reminder min priority", "1-5, or none."),
    "bedtime_penalty": FieldPolicy(INTERNAL, "Bedtime drift weight", "Cost per slot off preferred bedtime.", {S: 10, M: 25, L: 50}),
    "same_day_penalty": FieldPolicy(INTERNAL, "Same-day spread weight", "Cost of doubling up a task in a day.", {S: 500, M: 1000, L: 2000}),
    "sleep_target_penalty": FieldPolicy(INTERNAL, "Sleep target weight", "Cost per slot under sleep target.", {S: 500, M: 1000, L: 2500}),
    "presence_bonus": FieldPolicy(LOCKED, "Task fit bonus", "Protects fitting tasks over shuffling."),
    "sleep_min_penalty": FieldPolicy(LOCKED, "Min-sleep weight", "Protects minimum sleep."),
    "drop_priority_weight": FieldPolicy(LOCKED, "Drop priority weight", "Dropping cost per priority point."),
    "drop_difficulty_weight": FieldPolicy(LOCKED, "Drop difficulty weight", "Dropping cost per difficulty."),
    "drop_deadline_multiplier": FieldPolicy(LOCKED, "Deadline drop multiplier", "Protects deadlines."),
    "drop_sleep_weight": FieldPolicy(LOCKED, "Drop sleep weight", "Dropping cost per slot of lost sleep."),
    "drop_hard_flag_penalty": FieldPolicy(LOCKED, "Drop hard-flag penalty", "Cost per hard warning."),
    "plan_horizon_max_days": FieldPolicy(LOCKED, "Plan horizon", "Maximum planning days."),
    "shrink_steps": FieldPolicy(LOCKED, "Shrink steps", "Fractions tried when fitting a task."),
}

MODEL_DELTAS = {n: dict(p.deltas) for n, p in POLICY.items() if p.model_learnable}
