import math

from scheduler.models import ExtractedTask, MINUTES_PER_SLOT


def hours_to_slots(hours: float) -> int:  # NEW
    """1.5 -> 6. Rounds to the nearest 15 minutes (halves round up); minimum one slot."""
    slots = math.floor(hours * 60 / MINUTES_PER_SLOT + 0.5)
    if slots < 1:
        raise ValueError("duration must be at least 15 minutes")
    return slots


def edit_extracted_task(task: ExtractedTask, hours: float | None = None,  # NEW
                        priority: int | None = None, difficulty: int | None = None) -> ExtractedTask:
    """Return a copy of task with the given placeholders replaced (None = keep as is)."""
    changes = {}
    if hours is not None:
        changes["duration_slots"] = hours_to_slots(hours)
    if priority is not None:
        changes["priority"] = priority
    if difficulty is not None:
        changes["difficulty"] = difficulty
    return ExtractedTask.model_validate({**task.model_dump(), **changes})