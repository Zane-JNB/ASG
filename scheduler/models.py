from pydantic import BaseModel, Field, model_validator
from typing import Literal 

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
    @model_validator(mode="after")
    def end_after_start(self):
        if self.end_slot <= self.start_slot:
            raise ValueError("end_slot must be after start_slot")
        return self


class DynamicTask(BaseModel):
    title: str
    duration_slots: int = Field(gt=0)  # 4 slots = 1 hour
    priority: int = Field(ge=1, le=5)
    difficulty: int = Field(ge=1, le=5)
    splittable: bool = True
    max_session_slots: int = Field(default=8, gt=0) # 8 slots = 2 hours
    deadline_day: int | None = Field(default=None, ge=0)  # None = no deadline
    deadline_slot: int = Field(default=SLOTS_PER_DAY, gt=0, le=SLOTS_PER_DAY)  # exclusive
    earliest_start_day: int | None = Field(default=None, ge=0)  # None = no earliest bound
    earliest_start_slot: int = Field(default=0, ge=0, lt=SLOTS_PER_DAY)
    max_daily_slots: int | None = Field(default=None, gt=0)

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