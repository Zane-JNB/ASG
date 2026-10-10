from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

from scheduler.units import (
    MINUTES_PER_SLOT, SLOTS_PER_DAY, Weekday, parse_date, parse_due_time, parse_time, time_to_slot,
    weekday_name,
)

# Checked string fields: each is validated and normalised by its parser in scheduler.units.
ClockTime = Annotated[str, AfterValidator(parse_time)]  # "HH:MM", 24-hour
EndTime = Annotated[str, AfterValidator(lambda v: parse_time(v, end=True))]  # may also be "24:00"
DueTime = Annotated[str, AfterValidator(parse_due_time)]  # rounded down to its slot
IsoDate = Annotated[str, AfterValidator(parse_date)]  # "YYYY-MM-DD"

MAX_PRIORITY = 5


def axis_slot(day: int, slot: int) -> int:
    """A day's slot on the plan's continuous axis (day * 96 + slot)."""
    return day * SLOTS_PER_DAY + slot


class _OnAxis:
    """Something placed on one day of the plan: day, start_slot and end_slot (exclusive)."""
    day: int
    start_slot: int
    end_slot: int

    @property
    def span(self) -> tuple[int, int]:
        """(start, end) on the plan's continuous slot axis."""
        return axis_slot(self.day, self.start_slot), axis_slot(self.day, self.end_slot)


class FixedBlock(_OnAxis, BaseModel):
    title: str
    start_slot: int = Field(ge=0, lt=SLOTS_PER_DAY)
    end_slot: int = Field(gt=0, le=2 * SLOTS_PER_DAY)  # end is exclusive
    day: int = Field(default=0, ge=0)  # 0 = first day of the plan
    buffer_before: bool = True

    @model_validator(mode="after")
    def end_after_start(self):
        if self.end_slot <= self.start_slot:
            raise ValueError("end_slot must be after start_slot")
        return self

    def overlaps(self, other: "FixedBlock") -> bool:
        """True if the two blocks share any time. Back-to-back (end == next start) is fine."""
        (s1, e1), (s2, e2) = self.span, other.span
        return s1 < e2 and s2 < e1


class DynamicTask(BaseModel):
    title: str
    duration_slots: int = Field(gt=0)  # 4 slots = 1 hour
    priority: int = Field(ge=1, le=MAX_PRIORITY)
    difficulty: int = Field(default=3, ge=1, le=5)
    splittable: SkipJsonSchema[bool] = True
    may_cut_sleep: SkipJsonSchema[bool] = False  # the student let it take sleep below target (never below minimum)
    max_session_slots: int = Field(default=8, gt=0)  # 8 slots = 2 hours
    deadline_day: int | None = Field(default=None, ge=0)  # None = no deadline
    deadline_slot: int = Field(default=SLOTS_PER_DAY, gt=0, le=SLOTS_PER_DAY)  # exclusive
    earliest_start_day: int | None = Field(default=None, ge=0)  # None = no earliest bound
    earliest_start_slot: int = Field(default=0, ge=0, lt=SLOTS_PER_DAY)
    max_daily_slots: int | None = Field(default=None, gt=0)
    saved_id: SkipJsonSchema[int | None] = None  # id of the saved task this came from, if any

    @property
    def earliest_start(self) -> int:
        """The first slot it may start in, on the plan's continuous axis."""
        return 0 if self.earliest_start_day is None else axis_slot(self.earliest_start_day, self.earliest_start_slot)

    @property
    def deadline(self) -> int | None:
        """The slot it must end by (exclusive), on the plan's continuous axis; None = no deadline."""
        return None if self.deadline_day is None else axis_slot(self.deadline_day, self.deadline_slot)


class SleepRule(BaseModel):
    """One night of sleep. Night 0 starts on the evening of day 0."""
    night: int = Field(default=0, ge=0)
    length_slots: int = Field(default=32, gt=0)
    min_slots: int = Field(default=24, gt=0)
    earliest_bed: int = Field(default=84, ge=0, lt=2 * SLOTS_PER_DAY)
    latest_bed: int = Field(default=100, ge=0, lt=2 * SLOTS_PER_DAY)
    preferred_bed: int = Field(default=92, ge=0, lt=2 * SLOTS_PER_DAY)
    latest_wake: int | None = Field(default=None, ge=0, le=2 * SLOTS_PER_DAY)  # sleep must end by then; None = no limit
    latest_wake_reason: str | None = None  # what sets latest_wake, e.g. "'Work' at 06:00 the next morning"
    skip: bool = False

    @model_validator(mode="after")
    def check_rule(self):
        if self.min_slots > self.length_slots:
            raise ValueError("min_slots cannot be more than length_slots")
        if not (self.earliest_bed <= self.preferred_bed <= self.latest_bed):
            raise ValueError("need earliest_bed <= preferred_bed <= latest_bed")
        if self.latest_wake is not None and self.latest_wake < self.earliest_bed:
            raise ValueError("latest_wake cannot be before earliest_bed")
        return self

class ProfileSettings(BaseModel):
    """Per-student tuning knobs. Defaults match the constants the solver used to hardcode."""
    buffer_slots: int = Field(default=1, ge=0)
    presence_bonus: int = Field(default=10_000, gt=0)  # fitting a task beats moving things earlier
    sleep_min_penalty: int = Field(default=1_000_000, gt=0)  # per slot below minimum sleep
    sleep_target_penalty: int = Field(default=5_000, gt=0)  # per slot between minimum and target sleep
    bedtime_penalty: int = Field(default=50, gt=0)  # per slot away from the preferred bedtime
    same_day_penalty: int = Field(default=3_000, gt=0)  # two sessions of one task on the same day
    default_max_session_slots: int = Field(default=8, gt=0)  # 2h -- study session cap unless overridden
    default_sleep_length_slots: int = Field(default=32, gt=0)  # 8h target
    default_sleep_min_slots: int = Field(default=24, gt=0)  # 6h minimum
    default_earliest_bed: int = Field(default=84, ge=0, lt=2 * SLOTS_PER_DAY)  # 21:00
    default_latest_bed: int = Field(default=100, ge=0, lt=2 * SLOTS_PER_DAY)  # 01:00
    default_preferred_bed: int = Field(default=92, ge=0, lt=2 * SLOTS_PER_DAY)  # 23:00
    wake_buffer_slots: int = Field(default=4, ge=0)  # 1h to get ready between waking and the first class/commute
    drop_priority_weight: int = Field(default=10, ge=0)  # cost per lost slot, per priority point
    drop_difficulty_weight: int = Field(default=2, ge=0)  # cost per lost slot, per difficulty point
    drop_deadline_multiplier: int = Field(default=3, ge=1)  # losing time on a deadline task costs 3x
    drop_sleep_weight: int = Field(default=40, ge=0)  # per slot of sleep below target
    drop_hard_flag_penalty: int = Field(default=5_000, ge=0)  # per hard flag
    plan_horizon_max_days: int = Field(default=28, gt=0)
    shrink_steps: list[float] = Field(default_factory=lambda: [0.25, 0.5, 0.75])
    reminders_enabled: bool = True
    reminder_min_difficulty: int | None = Field(default=None, ge=1, le=5)  # None = ignore difficulty
    reminder_min_priority: int | None = Field(default=None, ge=1, le=5)  # None = ignore priority

    def default_sleep_rule(self, night: int, **overrides) -> "SleepRule":
        """Build a SleepRule for one night using this profile's defaults, with any field overridden."""
        fields = dict(
            night=night,
            length_slots=self.default_sleep_length_slots,
            min_slots=self.default_sleep_min_slots,
            earliest_bed=self.default_earliest_bed,
            latest_bed=self.default_latest_bed,
            preferred_bed=self.default_preferred_bed,
        )
        fields.update(overrides)
        return SleepRule(**fields)

class ScheduleWarning(BaseModel):
    severity: Literal["hard", "soft"]  # hard = something protected was given up
    kind: str  # e.g. "sleep_skipped", "sleep_short", "late_bedtime", "task_unscheduled"
    message: str

    @classmethod
    def hard(cls, kind: str, message: str) -> "ScheduleWarning":
        return cls(severity="hard", kind=kind, message=message)

    @classmethod
    def soft(cls, kind: str, message: str) -> "ScheduleWarning":
        return cls(severity="soft", kind=kind, message=message)

class ScheduledItem(_OnAxis, BaseModel):
    title: str
    start_slot: int
    end_slot: int  # exclusive
    kind: Literal["fixed", "task", "sleep"]
    day: int = 0
    saved_id: int | None = None  # task items: the saved task's id, if it has one

class TimeRange(BaseModel):
    """Base for a saved block with a start_time and end_time on one day (`when`: a weekday or a
    date): the end must come after the start."""

    @model_validator(mode="after")
    def times_make_sense(self):
        if time_to_slot(self.end_time) <= time_to_slot(self.start_time):
            raise ValueError("end_time must be after start_time")
        return self

    @property
    def slots(self) -> tuple[int, int]:
        """(start, end) slots within its day."""
        return time_to_slot(self.start_time), time_to_slot(self.end_time)

    def clashes(self, other: "TimeRange") -> bool:
        """Same kind on the same day, sharing time. Back to back is fine."""
        if type(self) is not type(other) or self.when != other.when:
            return False
        (s1, e1), (s2, e2) = self.slots, other.slots
        return s1 < e2 and s2 < e1

class WeeklyPattern(TimeRange):
    """A recurring fixed commitment on ONE specific day of the week -- e.g. 'Data Structures,
    Monday, 09:00-11:00'. If a class meets on several days, that's several WeeklyPattern
    entries, one per day -- this model deliberately cannot represent more than one day per
    entry, so extraction never has to judge whether two days' times are "close enough" to
    merge (a judgment call that proved unreliable in practice). This is expanded into
    concrete FixedBlocks for a specific plan by calendar_utils.expand_fixed_blocks, once the
    plan's real start date is known.
    """
    title: str
    day: Weekday
    start_time: ClockTime
    end_time: EndTime

    @property
    def when(self) -> Weekday:
        return self.day

class ExtractedTask(BaseModel):
    """A task/assignment found in an uploaded document, with a real calendar deadline.

    Only title, date and (if stated) due_time come from the document itself -- duration, priority, and difficulty
    aren't things a syllabus states, they're the student's judgment call, so they default to
    reasonable placeholders and are meant to be reviewed/adjusted before saving, not trusted
    as extracted fact.
    """
    title: str
    date: IsoDate  # a real calendar date, not a relative day index
    due_time: DueTime | None = Field(  # None = due at the end of that day
        default=None, description="Time the task is due, HH:MM 24-hour, only if the document states one")
    duration_slots: int = Field(default=4, gt=0)  # placeholder: 1 hour
    priority: int = Field(default=3, ge=1, le=MAX_PRIORITY)  # placeholder: medium
    difficulty: int = Field(default=3, ge=1, le=5)  # placeholder: medium
    splittable: SkipJsonSchema[bool] = True  # placeholder: can be split into sessions
    may_cut_sleep: SkipJsonSchema[bool] = False  # chosen when making room for it (see DynamicTask)
    completed_at: SkipJsonSchema[str | None] = None
    missed: SkipJsonSchema[bool] = False  # closed without being done (kept as history, like completed_at)

    @model_validator(mode="before")
    @classmethod
    def _midnight_is_the_day_before(cls, data):
        """A due time before 00:15 leaves no time that day: it means the end of the day before
        ('2026-10-06 00:00' -> '2026-10-05 24:00'). Bad values are left for the field checks."""
        if isinstance(data, dict) and isinstance(data.get("due_time"), str) and isinstance(data.get("date"), str):
            try:
                if time_to_slot(parse_time(data["due_time"], end=True)) == 0:
                    day = date.fromisoformat(data["date"]) - timedelta(days=1)
                    data = {**data, "date": day.isoformat(), "due_time": "24:00"}
            except ValueError:
                pass
        return data

    def due_label(self) -> str:
        """'2026-10-05' or '2026-10-05 10:00', as shown to the student."""
        return f"{self.date} {self.due_time}" if self.due_time else self.date

    def due_at(self) -> datetime:
        """When the task is due (no time = the end of that day)."""
        day = datetime.combine(date.fromisoformat(self.date), datetime.min.time())
        return day + timedelta(minutes=self.due_slot() * MINUTES_PER_SLOT)

    def due_slot(self) -> int:
        """due_time as an exclusive slot within its day (96 = end of day)."""
        return SLOTS_PER_DAY if self.due_time is None else time_to_slot(self.due_time)


class DatedBlock(TimeRange):
    """A one-off commitment on a specific calendar date with a specific time -- e.g. a module
    timetable that lists individual class sessions by date rather than a recurring weekly
    pattern. Distinct from WeeklyPattern (recurs every week) and ExtractedTask (a deadline
    with no fixed time): this has both a specific date AND a specific start/end time.
    """
    title: str
    date: IsoDate
    start_time: ClockTime
    end_time: EndTime

    @property
    def when(self) -> str:
        return self.date


class ExtractionResult(BaseModel):
    """What a document (image or PDF) yields: a recurring weekly timetable, one-off dated
    sessions with specific times, and/or a list of dated tasks/deadlines. Any list may be
    empty -- a syllabus might have only deadlines, a class-schedule screenshot might have
    only weekly patterns, a session-by-session timetable might have only dated blocks.
    """
    weekly_patterns: list[WeeklyPattern] = Field(default_factory=list)
    dated_blocks: list[DatedBlock] = Field(default_factory=list)
    tasks: list[ExtractedTask] = Field(default_factory=list)

class Commute(BaseModel):
    title: str = "Commute"
    start_time: ClockTime
    length_minutes: int = Field(gt=0, le=720)
    recurring: bool = False
    weekday: Weekday | None = None
    date: IsoDate | None = None  # one-time only
    end_date: IsoDate | None = None  # recurring only; None = until deleted
    skip_dates: list[IsoDate] = Field(default_factory=list)  # recurring only

    @model_validator(mode="after")
    def check_commute(self):
        if self.recurring:
            if self.weekday is None:
                raise ValueError("a recurring commute needs a weekday")
            if self.date is not None:
                raise ValueError("a recurring commute uses weekday, not date")
        else:
            if self.date is None:
                raise ValueError("a one-time commute needs a date")
            if self.weekday or self.end_date or self.skip_dates:
                raise ValueError("weekday, end_date and skip_dates are for recurring commutes only")
        return self

    def runs_on(self, day: date) -> bool:
        """True if this commute happens on that calendar date."""
        if not self.recurring:
            return day.isoformat() == self.date
        return (weekday_name(day) == self.weekday and day.isoformat() not in self.skip_dates
                and (self.end_date is None or day.isoformat() <= self.end_date))

class PlanAnchor(BaseModel):
    """The plan window for ONE solve: day 0 == start_date. Built fresh each run, never stored."""
    start_date: date
    num_days: int = Field(gt=0)

    def date_of(self, day: int) -> date:
        """The calendar date of a day index."""
        return self.start_date + timedelta(days=day)

    @property
    def dates(self) -> list[date]:
        """Every date in the window, day 0 first."""
        return [self.date_of(i) for i in range(self.num_days)]

class DropAction(BaseModel):
    task_index: int
    title: str
    chunks_cut: int
    total_chunks: int
    slots_lost: int
    slots_kept: int
    priority: int
    difficulty: int
    has_deadline: bool
    shrink: bool = False

    @property
    def is_full_drop(self) -> bool:
        return self.slots_kept == 0

class AddedTask(BaseModel):
    """How a proposal adds the new task: shortened by slots_cut for this plan only, and maybe with
    leave to use sleep below target (saved with the task)."""
    slots_cut: int = Field(default=0, ge=0)
    may_cut_sleep: bool = False


class DropProposal(BaseModel):
    rank: int = 0
    actions: list[DropAction]
    added: AddedTask | None  # None = "don't add the new task"
    score: float  # lower is better
    slots_freed: int
    sleep_sacrificed_slots: int
    flags: list[str]  # hard problems the student must see
    schedule: list[ScheduledItem]  # already solved

class DropReport(BaseModel):
    new_task_title: str
    fits_already: bool
    proposals: list[DropProposal]  # best first
    checks_used: int
    search_exhausted: bool  # False = stopped at the check limit
