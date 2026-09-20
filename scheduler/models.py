from pydantic import BaseModel, Field

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
    end_slot: int = Field(gt = 0, le=SLOTS_PER_DAY)  # end is exclusive


class DynamicTask(BaseModel):
    title: str
    duration_slots: int = Field(gt=0)  # 4 slots = 1 hour
    priority: int = Field(ge=1, le=5)
    difficulty: int = Field(ge=1, le=5)