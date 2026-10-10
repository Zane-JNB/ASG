import json
import os
from datetime import date, datetime
from typing import Annotated

from pydantic import Field, ValidationError, field_validator

from scheduler.calendar_utils import expand_fixed_blocks, overlap_lines, overlaps_between, window_through
from scheduler.db import DATED_BLOCKS, WEEKLY_PATTERNS, load_settings, replace_extraction
from scheduler.llm_backends import is_backend_failure
from scheduler.models import ExtractionResult
from scheduler.paths import CACHE_PATH
from scheduler.prompts import ask_missed, confirm
from scheduler.pdf_extraction import PartialExtraction, extract_document, page_count
from scheduler.review import review_extraction

class _SavedExtraction(ExtractionResult):
    """The replay copy: the extraction plus the PDF pages that weren't read, so replaying a
    partial import still warns that it is incomplete. Older copies have no failed_pages."""
    failed_pages: list[Annotated[int, Field(strict=True, ge=1)]] = Field(default_factory=list)

    @field_validator("failed_pages", mode="before")
    @classmethod
    def _none_is_empty(cls, v):
        return [] if v is None else v

    @field_validator("failed_pages")
    @classmethod
    def _sorted_unique(cls, v):
        return sorted(set(v))


MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".pdf": "application/pdf",
}


def _load_result(source_path, ask, show, extractor, cache_path):
    """Get an ExtractionResult from a saved .json (free) or a real image/PDF (LLM call).
    Returns (result, failed PDF page numbers, replay file holding it or None); result is None if
    the student declines the cost prompt or the call fails."""
    ext = os.path.splitext(source_path)[1].lower()
    if ext != ".json" and ext not in MEDIA_TYPES:
        show(f"Unsupported file type '{ext}'. Use png, jpg, jpeg, pdf, or a saved .json.")
        return None, [], None

    try:
        with open(source_path, "rb") as f:  # read once, reused below for the page count too
            file_bytes = f.read()
    except OSError as e:  # missing file, no permission, a folder...
        show(f"Could not read '{source_path}': {e.strerror or e}")
        return None, [], None

    if ext == ".json":
        try:
            # Notepad/PowerShell can save UTF-8 with a BOM; JSON parsers reject it, so drop it
            saved = _SavedExtraction.model_validate_json(file_bytes.removeprefix(b"\xef\xbb\xbf"))
            result = ExtractionResult.model_validate(saved.model_dump(exclude={"failed_pages"}))
        except ValidationError:  # bad JSON, wrong shape, or page numbers that aren't page numbers
            show(f"'{source_path}' is not a valid saved extraction.")
            return None, [], None
        return result, saved.failed_pages, source_path  # the replayed file still holds it

    page_note = ""
    if ext == ".pdf":  # each page can be its own call (text or rendered-image)
        try:
            page_note = f" This PDF has {page_count(file_bytes)} page(s); each may use its own call."
        except Exception:  # pdfplumber/pdfminer raise their own types for a broken or invalid PDF
            show(f"'{source_path}' could not be opened as a PDF.")
            return None, [], None
    if not confirm(ask, f"Groq (free tier, but a real API call).{page_note} Continue?", default=False):
        show("Aborted.")
        return None, [], None
    failed = []
    try:
        result = extractor(file_bytes, MEDIA_TYPES[ext])
    except PartialExtraction as e:  # keep the pages already read (and paid for)
        show(f"Warning: only part of the PDF was read -- {e}")
        result, failed = e.result, e.failed_pages
    except Exception as e:
        if not is_backend_failure(e):
            raise  # a bug in our own code: keep the traceback
        show(f"Extraction failed: {e}")  # report it, save nothing, don't crash
        return None, [], None

    tmp_path = os.fspath(cache_path) + ".tmp"
    try:  # saved before review; written aside first so a failed write keeps the old copy
        with open(tmp_path, "w", encoding="utf-8") as f:
            # failed_pages (read back by _SavedExtraction): replaying still warns it is incomplete
            f.write(json.dumps(result.model_dump(mode="json") | {"failed_pages": failed}, indent=2))
        os.replace(tmp_path, cache_path)
    except OSError as e:  # the replay copy is a convenience; don't lose the extraction over it
        show(f"Note: couldn't save a replay copy to '{cache_path}': {e.strerror or e}")
        if os.path.exists(cache_path):
            show(f"  '{cache_path}' still holds an EARLIER import; don't replay it for this document.")
        try:
            os.remove(tmp_path)  # it holds the student's timetable: don't leave it lying around
        except OSError:
            pass
        return result, failed, None
    return result, failed, cache_path


def _close_past_tasks(reviewed: ExtractionResult, now: datetime, ask, show) -> tuple[ExtractionResult, int]:
    """Tasks already due are saved as history, done or missed (the student says which), so they
    don't give an overdue warning on every plan. Returns (result, how many were closed)."""
    tasks, closed = [], 0
    for task in reviewed.tasks:
        if task.completed_at or task.due_at() > now:
            tasks.append(task)
            continue
        missed = ask_missed(ask, show, f"'{task.title}' was due {task.due_label()}.")
        tasks.append(task.model_copy(update={"completed_at": now.isoformat(timespec="minutes"), "missed": missed}))
        closed += 1
    return reviewed.model_copy(update={"tasks": tasks}), closed


def _clashes_with_saved(conn, student_id, reviewed: ExtractionResult, today: date) -> list[str]:
    """Overlaps between the blocks being imported and the saved table this import does NOT
    replace (weekly classes vs dated sessions), from today on. Clashes inside the import itself
    are already shown by the review. Lines as overlap_lines prints them; [] if none."""
    new_weekly, new_dated = reviewed.weekly_patterns, reviewed.dated_blocks
    if bool(new_weekly) == bool(new_dated):  # both replaced, or neither: nothing saved to clash with
        return []
    old_weekly = [] if new_weekly else [p for _, p in WEEKLY_PATTERNS.get(conn, student_id)]
    old_dated = [] if new_dated else [b for _, b in DATED_BLOCKS.get(conn, student_id)]
    anchor = window_through(today, [b.date for b in new_dated + old_dated])
    pairs = overlaps_between(expand_fixed_blocks(new_weekly, new_dated, anchor),
                             expand_fixed_blocks(old_weekly, old_dated, anchor))
    return overlap_lines(today, pairs)


def run_import(conn, student_id, source_path, ask=input, show=print,
               extractor=extract_document, cache_path=CACHE_PATH, now: datetime | None = None) -> bool:
    """Extract -> review -> replace the student's saved schedule items. True only if saved."""
    now = now or datetime.now()
    result, failed, kept_in = _load_result(source_path, ask, show, extractor, cache_path)
    if result is None:
        return False
    pages = ", ".join(map(str, failed))
    if not (result.weekly_patterns or result.dated_blocks or result.tasks):
        if failed:
            show("Nothing was found in the pages that were read. Your saved schedule is untouched.")
        else:
            show("Nothing was extracted. Your saved schedule is untouched.")
        return False

    cap = load_settings(conn, student_id).default_max_session_slots
    reviewed = review_extraction(result, ask=ask, show=show, session_cap=cap)
    if not (reviewed.weekly_patterns or reviewed.dated_blocks or reviewed.tasks):
        show("Nothing kept. Your saved schedule is untouched.")
        return False
    reviewed, closed = _close_past_tasks(reviewed, now, ask, show)
    if failed and not confirm(ask, f"Page(s) {pages} weren't read, so this import is "
                               "incomplete. Saving replaces your saved classes with only what was read. "
                               "Save anyway?", default=False):
        show("Not saved. Your saved schedule is untouched."
             + (f" What was read is kept in '{kept_in}'; replaying it warns again that it is incomplete."
                if kept_in else ""))
        return False
    clashes = _clashes_with_saved(conn, student_id, reviewed, now.date())
    if clashes:
        show("WARNING -- these overlap your saved classes/sessions:")
        for line in clashes:
            show(f"  {line}")
        if not confirm(ask, "Save anyway? Both are kept and plans avoid both (with a warning).", default=False):
            show("Not saved. Your saved schedule is untouched.")
            return False

    summary = replace_extraction(conn, student_id, reviewed)
    parts = []  # say what was replaced and what was left alone
    if summary["weekly"]:
        parts.append(f"{summary['weekly']} weekly (replaced old)")
    if summary["dated"]:
        parts.append(f"{summary['dated']} dated (replaced old)")
    if summary["tasks_added"] or summary["tasks_skipped"]:
        text = f"{summary['tasks_added']} task(s) added"
        if summary["tasks_skipped"]:
            text += f", {summary['tasks_skipped']} duplicate(s) skipped"
        if closed:
            text += f" ({closed} past one(s) kept as history)"
        parts.append(text)
    show("Saved: " + ", ".join(parts) + ".")
    return True