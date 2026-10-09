import base64
from datetime import datetime

from pydantic import ValidationError

from scheduler.models import WeeklyPattern, DatedBlock, ExtractedTask, ExtractionResult


def build_extraction_system_prompt() -> str:
    return (
        "You extract schedule information from an uploaded image or PDF of a student's timetable, "
        "class schedule, or syllabus. Identify three kinds of things:\n"
        "1. RECURRING weekly commitments (a class that meets on the same day every week, at the "
        "same time) -- title, the SINGLE day of the week it occurs on (Mon/Tue/Wed/Thu/Fri/Sat/Sun), "
        "start and end time. If a class meets on several days of the week, create ONE SEPARATE "
        "entry per day -- never combine multiple days into one entry, even if their times look "
        "similar or identical. One entry = one day, always.\n"
        "2. ONE-OFF dated sessions with a specific time (e.g. a module timetable listing individual "
        "class sessions by exact date, not a recurring weekly grid) -- title, the exact calendar "
        "date (YYYY-MM-DD), and start/end time. Read date headers in the document literally and "
        "exactly; do not shift a date by even one day.\n"
        "3. Dated tasks, assignments, or deadlines -- title, exact calendar date (YYYY-MM-DD), and "
        "due_time (HH:MM) only if the document states the time it is due; otherwise leave it out.\n\n"
        "CRITICAL: be skeptical of your own reading of which day-of-week column or date an item "
        "belongs to, especially in a dense grid/calendar layout where columns sit close together or "
        "text is small or overlapping. If you are not genuinely confident which day or column an "
        "item belongs to, OMIT that item entirely rather than guess. A missing item is a minor, "
        "recoverable gap. An item placed on the wrong day falsely claims a commitment exists that "
        "doesn't -- that is a much more serious error, and you must actively avoid it."
    )


_WEEKDAY_ORDER = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


def _sort_result(result: ExtractionResult) -> ExtractionResult:
    """Sort every list into a predictable, readable order -- done in code, not left to the
    model, since asking an LLM to also get the ordering right on top of everything else isn't
    reliable. weekly_patterns: by weekday then time. dated_blocks: by date then time.
    tasks: by due date and time.
    """
    weekly_patterns = sorted(result.weekly_patterns, key=lambda p: (_WEEKDAY_ORDER[p.day], p.start_time))
    dated_blocks = sorted(result.dated_blocks, key=lambda b: (b.date, b.start_time))
    tasks = sorted(result.tasks, key=lambda t: (t.date, t.due_slot()))
    return ExtractionResult(weekly_patterns=weekly_patterns, dated_blocks=dated_blocks, tasks=tasks)


_LOOSE_TIME_FORMATS = ("%H:%M", "%H:%M:%S", "%H", "%I%p", "%I %p", "%I:%M%p", "%I:%M %p")
_NO_TIME = {"", "0", "none", "null", "n/a", "na", "-", "tbd", "tba"}  # what models write for "no time stated"


def _task_with_loose_time(item: dict) -> list[ExtractedTask]:
    """[the task] with its due time read loosely, or flagged in the title when it can't be read.
    [] if the task is bad for another reason too."""
    raw_time = str(item["due_time"]).strip()
    if not (isinstance(item.get("title"), str) and item["title"].strip()):
        return []
    for fmt in _LOOSE_TIME_FORMATS:
        try:
            clock = datetime.strptime(raw_time.upper(), fmt).strftime("%H:%M")
            return [ExtractedTask(**{**item, "due_time": clock})]
        except (ValueError, TypeError):
            continue
    try:
        title = f"{item.get('title', '')} (CHECK due time: '{raw_time}')"
        return [ExtractedTask(**{**item, "title": title, "due_time": None})]
    except (ValidationError, TypeError):
        return []


def extraction_from_dict(raw: dict) -> ExtractionResult:
    """Validate each item on its own: one malformed entry (bad time, impossible date, not even
    an object) is dropped without discarding every other, otherwise valid, item. A task whose
    only problem is its due time is never dropped: a time like '5pm' or '17:00:00' is read as
    17:00; one that can't be read at all is left out and named in the title, so the student
    sees it in the review and sets it."""
    found = {}
    for key, model in (("weekly_patterns", WeeklyPattern), ("dated_blocks", DatedBlock), ("tasks", ExtractedTask)):
        found[key] = []
        for item in raw.get(key) or []:
            if model is ExtractedTask and isinstance(item, dict) and str(item.get("due_time", "")).strip().lower() in _NO_TIME:
                item = {**item, "due_time": None}
            try:
                found[key].append(model(**item))
            except (ValidationError, TypeError):
                if model is ExtractedTask and isinstance(item, dict) and item.get("due_time") is not None:
                    found[key] += _task_with_loose_time(item)
    return ExtractionResult(**found)


def extract_schedule(file_bytes: bytes, media_type: str, client=None) -> ExtractionResult:
    """Extract weekly patterns, dated sessions, and dated tasks from an uploaded image or PDF.

    media_type: e.g. "image/png", "image/jpeg", "application/pdf".
    client: optional OpenAI-SDK-shaped override (for testing). When omitted, dispatches
    through llm_backends.call_vision_llm, which calls Groq (images only, not PDFs).
    """
    image_base64 = base64.standard_b64encode(file_bytes).decode("utf-8")
    schema = ExtractionResult.model_json_schema()
    user_text = ("Extract the recurring weekly class schedule, any one-off dated sessions, "
                "and any dated tasks/deadlines from this document.")

    from scheduler.llm_backends import _groq_vision_call, call_vision_llm
    args = dict(system_prompt=build_extraction_system_prompt(), user_text=user_text,
                image_base64=image_base64, media_type=media_type,
                tool_name="extract_schedule", tool_schema=schema)
    raw = _groq_vision_call(**args, client=client) if client is not None else call_vision_llm(**args)
    return _sort_result(extraction_from_dict(raw))