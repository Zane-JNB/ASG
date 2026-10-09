import os

from pydantic import ValidationError

from scheduler.db import replace_extraction, load_settings
from scheduler.llm_backends import is_backend_failure
from scheduler.models import ExtractionResult
from scheduler.paths import CACHE_PATH
from scheduler.review import _confirm, review_extraction
from scheduler.schedule_extraction import extract_schedule
from scheduler.pdf_extraction import PartialExtraction, extract_schedule_from_pdf

MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".pdf": "application/pdf",
}


def _load_result(source_path, ask, show, extractor, cache_path):   
    """Get an ExtractionResult from a saved .json (free) or a real image/PDF (LLM call).
    Returns (result, failed PDF page numbers); result is None if the student declines the
    cost prompt or the call fails."""
    ext = os.path.splitext(source_path)[1].lower()
    if ext != ".json" and ext not in MEDIA_TYPES:
        show(f"Unsupported file type '{ext}'. Use png, jpg, jpeg, pdf, or a saved .json.")
        return None, []

    try:
        with open(source_path, "rb") as f:  #   -- read once, reused below for the page count too
            file_bytes = f.read()
    except OSError as e:  # missing file, no permission, a folder...
        show(f"Could not read '{source_path}': {e.strerror or e}")
        return None, []

    if ext == ".json":
        try:
            # Notepad/PowerShell can save UTF-8 with a BOM; JSON parsers reject it, so drop it
            return ExtractionResult.model_validate_json(file_bytes.removeprefix(b"\xef\xbb\xbf")), []
        except ValidationError:  # bad JSON or wrong shape
            show(f"'{source_path}' is not a valid saved extraction.")
            return None, []

    page_note = ""
    if ext == ".pdf":  #   -- each page can be its own call (text or rendered-image)
        import io
        import pdfplumber
        try:
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:  #   -- from bytes, matches
                num_pages = len(pdf.pages)                        # extract_schedule_from_pdf exactly
        except Exception:  # pdfplumber/pdfminer raise their own types for a broken or invalid PDF
            show(f"'{source_path}' could not be opened as a PDF.")
            return None, []
        page_note = f" This PDF has {num_pages} page(s); each may use its own call."
    if not _confirm(ask, f"Groq (free tier, but a real API call).{page_note} Continue?", default=False):
        show("Aborted.")
        return None, []
    failed = []
    try:
        if ext == ".pdf" and extractor is extract_schedule:  #   -- default PDF path avoids
            result = extract_schedule_from_pdf(file_bytes)   # Groq's vision model rejecting PDFs
        else:
            result = extractor(file_bytes, MEDIA_TYPES[ext])
    except PartialExtraction as e:  # keep the pages already read (and paid for)
        show(f"Warning: only part of the PDF was read -- {e}")
        result, failed = e.result, e.failed_pages
    except Exception as e:
        if not is_backend_failure(e):
            raise  # a bug in our own code: keep the traceback
        show(f"Extraction failed: {e}")  # report it, save nothing, don't crash
        return None, []

    try:
        with open(cache_path, "w", encoding="utf-8") as f:  #   -- saved before review
            f.write(result.model_dump_json(indent=2))
    except OSError as e:  # the replay copy is a convenience; don't lose the extraction over it
        show(f"Note: couldn't save a replay copy to '{cache_path}': {e.strerror or e}")
    return result, failed


def run_import(conn, student_id, source_path, ask=input, show=print,
               extractor=extract_schedule, cache_path=CACHE_PATH) -> bool:   
    """Extract -> review -> replace the student's saved schedule items. True only if saved."""
    result, failed = _load_result(source_path, ask, show, extractor, cache_path)
    if result is None:
        return False
    if not (result.weekly_patterns or result.dated_blocks or result.tasks):
        show("Nothing was extracted. Your saved schedule is untouched.")
        return False

    cap = load_settings(conn, student_id).default_max_session_slots  
    reviewed = review_extraction(result, ask=ask, show=show, session_cap=cap)  
    if not (reviewed.weekly_patterns or reviewed.dated_blocks or reviewed.tasks):
        show("Nothing kept. Your saved schedule is untouched.")
        return False
    if failed and not _confirm(ask, f"Page(s) {', '.join(map(str, failed))} weren't read, so this import is "
                               "incomplete. Saving replaces your saved classes with only what was read. "
                               "Save anyway?", default=False):
        show("Not saved. Your saved schedule is untouched. What was read is kept in the replay copy.")
        return False

    summary = replace_extraction(conn, student_id, reviewed)  #     returns a dict, not None
    parts = []  #   -- say what was replaced and what was left alone
    if summary["weekly"]:
        parts.append(f"{summary['weekly']} weekly (replaced old)")
    if summary["dated"]:
        parts.append(f"{summary['dated']} dated (replaced old)")
    if summary["tasks_added"] or summary["tasks_skipped"]:
        text = f"{summary['tasks_added']} task(s) added"
        if summary["tasks_skipped"]:
            text += f", {summary['tasks_skipped']} duplicate(s) skipped"
        parts.append(text)
    show("Saved: " + ", ".join(parts) + ".")
    return True