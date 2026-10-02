import os

from scheduler.db import replace_extraction, load_settings
from scheduler.models import ExtractionResult
from scheduler.review import _confirm, review_extraction
from scheduler.schedule_extraction import extract_schedule
from scheduler.pdf_extraction import extract_schedule_from_pdf

CACHE_PATH = "last_extraction.json"  #   -- last raw extraction, so the review can be replayed free

MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".pdf": "application/pdf",
}


def _load_result(source_path, ask, show, extractor, cache_path):   
    """Get an ExtractionResult from a saved .json (free) or a real image/PDF (LLM call).
    Returns None if the student declines the cost prompt or the call fails."""
    if source_path.lower().endswith(".json"):
        with open(source_path, encoding="utf-8") as f:
            return ExtractionResult.model_validate_json(f.read())

    ext = os.path.splitext(source_path)[1].lower()
    if ext not in MEDIA_TYPES:
        show(f"Unsupported file type '{ext}'. Use png, jpg, jpeg, pdf, or a saved .json.")
        return None

    with open(source_path, "rb") as f:  #   -- read once, reused below for the page count too
        file_bytes = f.read()

    backend = os.environ.get("LLM_BACKEND", "fake")
    if backend != "fake":
        cost_note = "paid" if backend == "anthropic" else "free-tier but a real API call"
        page_note = ""
        if ext == ".pdf":  #   -- each page can be its own call (text or rendered-image)
            import io
            import pdfplumber
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:  #   -- from bytes, matches
                num_pages = len(pdf.pages)                        # extract_schedule_from_pdf exactly
            page_note = f" This PDF has {num_pages} page(s); each may use its own call."
        if not _confirm(ask, f"LLM_BACKEND={backend} ({cost_note}).{page_note} Continue?", default=False):
            show("Aborted.")
            return None
    try:
        if ext == ".pdf" and extractor is extract_schedule:  #   -- default PDF path avoids
            result = extract_schedule_from_pdf(file_bytes)   # Groq's vision model rejecting PDFs
        else:
            result = extractor(file_bytes, MEDIA_TYPES[ext])
    except ValueError as e:
        show(f"Extraction failed: {e}")
        return None

    with open(cache_path, "w", encoding="utf-8") as f:  #   -- saved before review
        f.write(result.model_dump_json(indent=2))
    return result


def run_import(conn, student_id, source_path, ask=input, show=print,
               extractor=extract_schedule, cache_path=CACHE_PATH) -> bool:   
    """Extract -> review -> replace the student's saved schedule items. True only if saved."""
    result = _load_result(source_path, ask, show, extractor, cache_path)
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