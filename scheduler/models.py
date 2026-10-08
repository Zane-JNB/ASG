from pydantic import BaseModel, Field, model_validator
from typing import Literal 
from datetime import date, timedelta
from pydantic.json_schema import SkipJsonSchema

MINUTES_PER_SLOT = 15
SLOTS_PER_DAY = 24 * 60 // MINUTES_PER_SLOT  # 96 slots in a day

def time_to_slot(hhmm: str) -> int:
    """'09:30' -> 38"""
    h, m = hhmm.split(":")
    return (int(h) * 60 + int(m)) // MINUTES_PER_SLOT

def slot_to_time(slot: int) -> str:
    """38 -> '09:30'"""
    minutes = slot * MINUTES_PER_SLOT
    return f"{minutes // 60:02d}:{minutes % 60:02d}"

class FixedBlock(BaseModel):
    title: str
    start_slot: int = Field(ge = 0, lt=SLOTS_PER_DAY)
    end_slot: int = Field(gt = 0, le=2 * SLOTS_PER_DAY)  # end is exclusive
    day: int = Field(default=0, ge=0)  # 0 = first day of the plan
    buffer_before: bool = True
    @model_validator(mode="after")
    def end_after_start(self):
        if self.end_slot <= self.start_slot:
            raise ValueError("end_slot must be after start_slot")
        return self


class DynamicTask(BaseModel):
    title: str
    duration_slots: int = Field(gt=0)  # 4 slots = 1 hour
    priority: int = Field(ge=1, le=5)
    difficulty: int = Field(default=3, ge=1, le=5)
    splittable: SkipJsonSchema[bool] = True
    max_session_slots: int = Field(default=8, gt=0) # 8 slots = 2 hours
    deadline_day: int | None = Field(default=None, ge=0)  # None = no deadline
    deadline_slot: int = Field(default=SLOTS_PER_DAY, gt=0, le=SLOTS_PER_DAY)  # exclusive
    earliest_start_day: int | None = Field(default=None, ge=0)  # None = no earliest bound
    earliest_start_slot: int = Field(default=0, ge=0, lt=SLOTS_PER_DAY)
    max_daily_slots: int | None = Field(default=None, gt=0)
    completed_at: SkipJsonSchema[str | None] = None
    saved_id: SkipJsonSchema[int | None] = None  # id of the saved task this came from, if any

class SleepRule(BaseModel):  #
    """One night of sleep. Night 0 starts on the evening of day 0.""" 
    night: int = Field(default=0, ge=0)  
    length_slots: int = Field(default=32, gt=0) 
    min_slots: int = Field(default=24, gt=0) 
    earliest_bed: int = Field(default=84, ge=0, lt=2 * SLOTS_PER_DAY)
    latest_bed: int = Field(default=100, ge=0, lt=2 * SLOTS_PER_DAY) 
    preferred_bed: int = Field(default=92, ge=0, lt=2 * SLOTS_PER_DAY)
    skip: bool = False  
 
    @model_validator(mode="after") 
    def check_rule(self):  
        if self.min_slots > self.length_slots:  
            raise ValueError("min_slots cannot be more than length_slots")  
        if not (self.earliest_bed <= self.preferred_bed <= self.latest_bed):  
            raise ValueError("need earliest_bed <= preferred_bed <= latest_bed")  
        return self 

class ProfileSettings(BaseModel):  
    """Per-student tuning knobs. Defaults match the constants the solver used to hardcode."""   
    buffer_slots: int = Field(default=1, ge=0)
    presence_bonus: int = Field(default=10_000, gt=0)  #   fitting a task beats moving things earlier
    sleep_min_penalty: int = Field(default=1_000_000, gt=0)  #   per slot below minimum sleep
    sleep_target_penalty: int = Field(default=5_000, gt=0)  #   per slot between minimum and target sleep
    bedtime_penalty: int = Field(default=50, gt=0)  #  per slot away from the preferred bedtime
    same_day_penalty: int = Field(default=3_000, gt=0)  #  two sessions of one task on the same day
    default_max_session_slots: int = Field(default=8, gt=0)  #  2h -- study session cap unless overridden
    default_sleep_length_slots: int = Field(default=32, gt=0)  #  8h target
    default_sleep_min_slots: int = Field(default=24, gt=0)  #  6h minimum
    default_earliest_bed: int = Field(default=84, ge=0, lt=2 * SLOTS_PER_DAY)  #  21:00
    default_latest_bed: int = Field(default=100, ge=0, lt=2 * SLOTS_PER_DAY)  #  01:00
    default_preferred_bed: int = Field(default=92, ge=0, lt=2 * SLOTS_PER_DAY)  #  23:00
    drop_priority_weight: int = Field(default=10, ge=0)  #    cost per lost slot, per priority point
    drop_difficulty_weight: int = Field(default=2, ge=0)  #  
    drop_deadline_multiplier: int = Field(default=3, ge=1)  #    losing time on a deadline task costs 3x
    drop_sleep_weight: int = Field(default=40, ge=0)  #    per slot of sleep below target
    drop_hard_flag_penalty: int = Field(default=5_000, ge=0)  #    per hard flag
    plan_horizon_max_days: int = Field(default=28, gt=0)
    shrink_steps: list[float] = Field(default_factory=lambda: [0.25, 0.5, 0.75])
    reminders_enabled: bool = True
    reminder_min_difficulty: int | None = Field(default=None, ge=1, le=5)  #    None = ignore difficulty
    reminder_min_priority: int | None = Field(default=None, ge=1, le=5)  #    None = ignore priority

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
    severity: Literal["hard", "soft"]  #  hard = something protected was given up
    kind: str  #  e.g. "sleep_skipped", "sleep_short", "late_bedtime", "task_unscheduled"
    message: str

class ScheduledItem(BaseModel):
    title: str
    start_slot: int
    end_slot: int  # exclusive
    kind: str      # "fixed" or "task"
    day: int = 0
    saved_id: int | None = None  # task items: the saved task's id, if it has one

class Exam(BaseModel):
    title: str
    day: int = Field(ge=0)
    slot: int = Field(default=SLOTS_PER_DAY, gt=0, le=SLOTS_PER_DAY)  # when it starts that day
    difficulty: int = Field(ge=1, le=5)
    priority: int = Field(default=4, ge=1, le=5)  # priority given to the generated study task


class StudyBand(BaseModel):
    """One difficulty band's study policy, e.g. 'hard tests get a week, 2-4h/day'."""
    days_before: int = Field(gt=0)
    min_hours_per_day: float = Field(gt=0)
    max_hours_per_day: float = Field(gt=0)

    @model_validator(mode="after")
    def hours_make_sense(self):
        if self.min_hours_per_day > self.max_hours_per_day:
            raise ValueError("min_hours_per_day cannot exceed max_hours_per_day")
        return self


class StudyPlanRule(BaseModel):
    """Maps exam difficulty (1-5) to a study band. Defaults are Zane's personal rule."""
    easy: StudyBand = Field(default_factory=lambda: StudyBand(
        days_before=2, min_hours_per_day=2, max_hours_per_day=4))
    medium: StudyBand = Field(default_factory=lambda: StudyBand(
        days_before=5, min_hours_per_day=2, max_hours_per_day=4))
    hard: StudyBand = Field(default_factory=lambda: StudyBand(
        days_before=7, min_hours_per_day=2, max_hours_per_day=4))

    def band_for(self, difficulty: int) -> StudyBand:
        if difficulty <= 2:
            return self.easy
        if difficulty == 3:
            return self.medium
        return self.hard

class WeeklyPattern(BaseModel):
    """A recurring fixed commitment on ONE specific day of the week -- e.g. 'Data Structures,
    Monday, 09:00-11:00'. If a class meets on several days, that's several WeeklyPattern
    entries, one per day -- this model deliberately cannot represent more than one day per
    entry, so extraction never has to judge whether two days' times are "close enough" to
    merge (a judgment call that proved unreliable in practice). This is expanded into
    concrete FixedBlocks for a specific plan by calendar_utils.expand_weekly_pattern, once the
    plan's real start date is known.
    """
    title: str
    day: Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    start_time: str  # "HH:MM", 24-hour
    end_time: str  # "HH:MM", 24-hour

    @model_validator(mode="after")
    def times_make_sense(self):
        if time_to_slot(self.end_time) <= time_to_slot(self.start_time):
            raise ValueError("end_time must be after start_time")
        return self

class ExtractedTask(BaseModel):
    """A task/assignment found in an uploaded document, with a real calendar deadline.

    Only title and date come from the document itself -- duration, priority, and difficulty
    aren't things a syllabus states, they're the student's judgment call, so they default to
    reasonable placeholders and are meant to be reviewed/adjusted before saving, not trusted
    as extracted fact.
    """
    title: str
    date: str  # "YYYY-MM-DD" -- a real calendar date, not a relative day index
    duration_slots: int = Field(default=4, gt=0)  # placeholder: 1 hour
    priority: int = Field(default=3, ge=1, le=5)  # placeholder: medium
    difficulty: int = Field(default=3, ge=1, le=5)  # placeholder: medium
    splittable: SkipJsonSchema[bool] = True  # placeholder: can be split into sessions
    reminders_enabled: bool = True   
    reminder_min_difficulty: int | None = Field(default=None, ge=1, le=5)  #    None = ignore difficulty
    reminder_min_priority: int | None = Field(default=None, ge=1, le=5)  #    None = ignore priority
    completed_at: SkipJsonSchema[str | None] = None


class DatedBlock(BaseModel):
    """A one-off commitment on a specific calendar date with a specific time -- e.g. a module
    timetable that lists individual class sessions by date rather than a recurring weekly
    pattern. Distinct from WeeklyPattern (recurs every week) and ExtractedTask (a deadline
    with no fixed time): this has both a specific date AND a specific start/end time.
    """
    title: str
    date: str  # "YYYY-MM-DD"
    start_time: str  # "HH:MM", 24-hour
    end_time: str  # "HH:MM", 24-hour

    @model_validator(mode="after")
    def times_make_sense(self):
        if time_to_slot(self.end_time) <= time_to_slot(self.start_time):
            raise ValueError("end_time must be after start_time")
        return self


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
    start_time: str  # "HH:MM", 24-hour
    length_minutes: int = Field(gt=0, le=720)
    recurring: bool = False
    weekday: Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] | None = None
    date: str | None = None  # one-time only
    end_date: str | None = None  # recurring only; None = until deleted
    skip_dates: list[str] = Field(default_factory=list)  # recurring only

    @model_validator(mode="after")
    def check_commute(self):
        h, m = self.start_time.split(":")
        if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
            raise ValueError("start_time must be a valid HH:MM")
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
        for d in [self.date, self.end_date, *self.skip_dates]:
            if d is not None:
                date.fromisoformat(d)  # raises on a bad date
        return self

class PlanAnchor(BaseModel):  
    """The plan window for ONE solve: day 0 == start_date. Built fresh each run, never stored."""
    start_date: date
    num_days: int = Field(gt=0)

    @classmethod
    def from_today(cls, num_days: int, today: date | None = None) -> "PlanAnchor":
        return cls(start_date=today or date.today(), num_days=num_days)

    @property
    def end_date(self) -> date:  # last day INCLUDED in the plan
        return self.start_date + timedelta(days=self.num_days - 1)

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

class DropProposal(BaseModel):   
    rank: int = 0
    actions: list[DropAction]
    new_task_added: bool  # False = "don't add the new task"
    score: float  # lower is better
    slots_freed: int
    sleep_sacrificed_slots: int
    flags: list[str]  # hard problems the student must see
    schedule: list[ScheduledItem]  # already solved
    new_task_slots_cut: int = 0

class DropReport(BaseModel):  
    new_task_title: str
    fits_already: bool
    proposals: list[DropProposal]  # best first
    checks_used: int
    search_exhausted: bool  # False = stopped at the check limit