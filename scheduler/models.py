from pydantic import BaseModel, Field, model_validator

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

class ScheduledItem(BaseModel):
    title: str
    start_slot: int
    end_slot: int  # exclusive
    kind: str      # "fixed" or "task"
    day: int = 0