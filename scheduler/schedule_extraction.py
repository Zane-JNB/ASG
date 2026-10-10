"""An uploaded document -> ExtractionResult: the prompt, the request, and a lenient reading of
the model's answer (one bad entry is dropped, never the whole answer)."""
import base64
import json
from pydantic import ValidationError

from scheduler.llm_backends import NONE_WORDS, BadModelOutput, as_items, call_vision_llm
from scheduler.models import DatedBlock, ExtractedTask, ExtractionResult, WeeklyPattern
from scheduler.units import WEEKDAYS, parse_loose_time

TOOL_NAME = "extract_schedule"


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


def combine(results: list[ExtractionResult]) -> ExtractionResult:
    """Every result's items in one, each list in a predictable, readable order -- done in code, not
    left to the model, since asking an LLM to also get the ordering right isn't reliable.
    weekly_patterns: by weekday then time. dated_blocks: by date then time. tasks: by due date and time."""
    return ExtractionResult(
        weekly_patterns=sorted((p for r in results for p in r.weekly_patterns),
                               key=lambda p: (WEEKDAYS.index(p.day), p.start_time)),
        dated_blocks=sorted((b for r in results for b in r.dated_blocks), key=lambda b: (b.date, b.start_time)),
        tasks=sorted((t for r in results for t in r.tasks), key=lambda t: (t.date, t.due_slot())),
    )


_NO_TIME = NONE_WORDS | {"0", "tbd", "tba"}  # what models write for "no time stated"


def _task_with_loose_time(item: dict) -> list[ExtractedTask]:
    """[the task] with its due time read loosely, or flagged in the title when it can't be read.
    [] if the task is bad for another reason too."""
    raw_time = str(item["due_time"]).strip()
    if not (isinstance(item.get("title"), str) and item["title"].strip()):
        return []
    try:
        return [ExtractedTask(**{**item, "due_time": parse_loose_time(raw_time)})]
    except (ValueError, TypeError):  # unreadable time, or the task is bad for another reason too
        pass
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
    sees it in the review and sets it. An answer, or a list key, in a shape that can't be read
    raises BadModelOutput (see llm_backends.as_items)."""
    if not isinstance(raw, dict):
        raise BadModelOutput(f"The model sent an answer in the wrong shape ({type(raw).__name__}). Try again.")
    found = {}
    for key, model in (("weekly_patterns", WeeklyPattern), ("dated_blocks", DatedBlock), ("tasks", ExtractedTask)):
        found[key] = []
        for item in as_items(key, raw.get(key)):
            if isinstance(item, str) and item.strip().startswith("{"):  # one entry sent as JSON text
                try:
                    item = json.loads(item)
                except ValueError:
                    pass
            if model is ExtractedTask and isinstance(item, dict) and str(item.get("due_time", "")).strip().lower() in _NO_TIME:
                item = {**item, "due_time": None}
            try:
                found[key].append(model(**item))
            except (ValidationError, TypeError):
                if model is ExtractedTask and isinstance(item, dict) and item.get("due_time") is not None:
                    found[key] += _task_with_loose_time(item)
    return ExtractionResult(**found)


def image_request(image_bytes: bytes, media_type: str, what: str = "this document") -> dict:
    """The vision call's arguments for one image (llm_backends.call_vision_llm)."""
    return dict(system_prompt=build_extraction_system_prompt(),
                user_text=("Extract the recurring weekly class schedule, any one-off dated sessions, "
                           f"and any dated tasks/deadlines from {what}."),
                image_base64=base64.standard_b64encode(image_bytes).decode("utf-8"),
                media_type=media_type, tool_name=TOOL_NAME, tool_schema=ExtractionResult.model_json_schema())


def extract_schedule(file_bytes: bytes, media_type: str, client=None) -> ExtractionResult:
    """Extract weekly patterns, dated sessions, and dated tasks from an uploaded image, through
    llm_backends.call_vision_llm (Groq: images only; PDFs go through pdf_extraction).
    client: optional OpenAI-SDK-shaped stand-in (for testing)."""
    return combine([extraction_from_dict(call_vision_llm(**image_request(file_bytes, media_type), client=client))])