import base64

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
        "3. Dated tasks, assignments, or deadlines with no fixed time -- title and exact calendar "
        "date (YYYY-MM-DD).\n\n"
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
    tasks: by date.
    """
    weekly_patterns = sorted(result.weekly_patterns, key=lambda p: (_WEEKDAY_ORDER[p.day], p.start_time))
    dated_blocks = sorted(result.dated_blocks, key=lambda b: (b.date, b.start_time))
    tasks = sorted(result.tasks, key=lambda t: t.date)
    return ExtractionResult(weekly_patterns=weekly_patterns, dated_blocks=dated_blocks, tasks=tasks)


def extract_schedule(file_bytes: bytes, media_type: str, client=None) -> ExtractionResult:
    """Extract weekly patterns, dated sessions, and dated tasks from an uploaded image or PDF.

    media_type: e.g. "image/png", "image/jpeg", "application/pdf".
    client: optional Anthropic-SDK-shaped override (for testing). When omitted, dispatches
    through llm_backends.call_vision_llm, which reads LLM_BACKEND from the environment.
    """
    image_base64 = base64.standard_b64encode(file_bytes).decode("utf-8")
    schema = ExtractionResult.model_json_schema()
    user_text = ("Extract the recurring weekly class schedule, any one-off dated sessions, "
                "and any dated tasks/deadlines from this document.")

    from scheduler.llm_backends import _anthropic_vision_call, call_vision_llm
    args = dict(system_prompt=build_extraction_system_prompt(), user_text=user_text,
                image_base64=image_base64, media_type=media_type,
                tool_name="extract_schedule", tool_schema=schema)
    raw = _anthropic_vision_call(**args, client=client) if client is not None else call_vision_llm(**args)

    # validate each item individually -- one malformed entry (bad time format, ambiguous
    # date) shouldn't discard every other, otherwise valid, item the model found
    weekly_patterns = []
    for item in raw.get("weekly_patterns", []):
        try:
            weekly_patterns.append(WeeklyPattern(**item))
        except ValidationError:
            continue

    dated_blocks = []
    for item in raw.get("dated_blocks", []):
        try:
            dated_blocks.append(DatedBlock(**item))
        except ValidationError:
            continue

    tasks = []
    for item in raw.get("tasks", []):
        try:
            tasks.append(ExtractedTask(**item))
        except ValidationError:
            continue

    return _sort_result(ExtractionResult(weekly_patterns=weekly_patterns, dated_blocks=dated_blocks, tasks=tasks))