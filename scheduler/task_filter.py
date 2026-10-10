"""Which tasks the student wants check-in reminders for (profile settings, not per task)."""
from scheduler.models import DynamicTask, ExtractedTask, ProfileSettings


def task_matters(task: ExtractedTask | DynamicTask, min_difficulty: int | None = None,
                 min_priority: int | None = None) -> bool:
    """True if the task meets EITHER threshold (difficulty at/above, or priority at/above).
    A threshold of None is ignored; with both None, every task matters. Works on saved and planned
    tasks alike."""
    if min_difficulty is None and min_priority is None:
        return True
    return ((min_difficulty is not None and task.difficulty >= min_difficulty)
            or (min_priority is not None and task.priority >= min_priority))


def wants_reminder(task: ExtractedTask | DynamicTask, settings: ProfileSettings) -> bool:
    return settings.reminders_enabled and task_matters(
        task, settings.reminder_min_difficulty, settings.reminder_min_priority)


def describe_reminders(settings: ProfileSettings) -> str:
    if not settings.reminders_enabled:
        return "Reminders are off."
    parts = []
    if settings.reminder_min_difficulty is not None:
        parts.append(f"difficulty {settings.reminder_min_difficulty}+")
    if settings.reminder_min_priority is not None:
        parts.append(f"priority {settings.reminder_min_priority}+")
    return "Reminders are on for " + (" or ".join(parts) + " tasks." if parts else "all tasks.")