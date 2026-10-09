"""PDF -> ExtractionResult, without needing a paid vision model.

Strategy per page:
  - If pdfplumber finds real text, send that TEXT to an LLM (via llm_backends.call_llm,
    same llm_backends dispatch as reflections -- works on the free Groq tier today).
  - If a page has little/no extractable text (a scanned page, or a page that's mostly a
    picture), render that page to an image with pypdfium2 and reuse the EXISTING image
    extraction path (extract_schedule / call_vision_llm) -- the same path already used
    for photographed timetables.
Both libraries used here (pdfplumber, pypdfium2) are permissively licensed (MIT / Apache-BSD),
unlike PyMuPDF (AGPL), which matters if this app is ever sold.

Nothing here calls a specific provider directly: everything goes through llm_backends,
so adding another provider later (model routing, 4.0) needs no change here.
"""

import pdfplumber
import pypdfium2 as pdfium

from scheduler.models import WeeklyPattern, DatedBlock, ExtractedTask, ExtractionResult
from scheduler.schedule_extraction import _sort_result, build_extraction_system_prompt

MIN_TEXT_CHARS = 40  # below this, treat the page as "no usable text" and render it instead
RENDER_SCALE = 2.0   # ~144 DPI; enough to read timetable text without huge file sizes


def _extraction_from_dict(raw: dict) -> ExtractionResult:
    """Same per-item validation as extract_schedule: one bad entry doesn't discard the rest."""
    weekly_patterns, dated_blocks, tasks = [], [], []
    for item in raw.get("weekly_patterns", []):
        try:
            weekly_patterns.append(WeeklyPattern(**item))
        except Exception:
            continue
    for item in raw.get("dated_blocks", []):
        try:
            dated_blocks.append(DatedBlock(**item))
        except Exception:
            continue
    for item in raw.get("tasks", []):
        try:
            tasks.append(ExtractedTask(**item))
        except Exception:
            continue
    return ExtractionResult(weekly_patterns=weekly_patterns, dated_blocks=dated_blocks, tasks=tasks)


def _merge(results: list[ExtractionResult]) -> ExtractionResult:
    return ExtractionResult(
        weekly_patterns=[p for r in results for p in r.weekly_patterns],
        dated_blocks=[b for r in results for b in r.dated_blocks],
        tasks=[t for r in results for t in r.tasks],
    )

def _is_reflection_fallback(raw: dict) -> bool:   
    """call_llm's Groq backend soft-degrades a malformed tool call into a REFLECTION-shaped
    stub ({"summary": ..., "proposals": []}) -- correct for reflections but wrong for
    extraction: no weekly_patterns/dated_blocks/tasks keys, so it silently looks like
    "found nothing" instead of "this call failed"."""
    return "proposals" in raw and not any(k in raw for k in ("weekly_patterns", "dated_blocks", "tasks"))

def extract_text_page(page_text: str, call_text_llm) -> ExtractionResult:
    """One page's extracted text -> ExtractionResult. call_text_llm is llm_backends.call_llm
    (injectable for tests, same as extract_schedule takes an injectable client)."""
    schema = ExtractionResult.model_json_schema()
    user_message = (
        "This text was extracted from one page of a student's schedule document. Extract the "
        "recurring weekly class schedule, any one-off dated sessions, and any dated tasks/"
        "deadlines it describes. If this page has nothing relevant, return empty lists.\n\n"
        f"--- PAGE TEXT ---\n{page_text}"
    )
    raw = call_text_llm(
        system_prompt=build_extraction_system_prompt(),
        user_message=user_message,
        tool_name="extract_schedule",
        tool_schema=schema,
    )
    if _is_reflection_fallback(raw): 
        raise ValueError("the model's response didn't match the expected schema for this page")
    return _extraction_from_dict(raw)


def extract_image_page(image_bytes: bytes, call_vision) -> ExtractionResult:
    """One rendered page image -> ExtractionResult, via the existing vision path."""
    schema = ExtractionResult.model_json_schema()
    raw = call_vision(
        system_prompt=build_extraction_system_prompt(),
        user_text="Extract the recurring weekly class schedule, any one-off dated sessions, "
                 "and any dated tasks/deadlines from this document page.",
        image_base64=__import__("base64").standard_b64encode(image_bytes).decode("utf-8"),
        media_type="image/png",
        tool_name="extract_schedule",
        tool_schema=schema,
    )
    return _extraction_from_dict(raw)


def extract_schedule_from_pdf(file_bytes: bytes, call_text_llm=None, call_vision=None) -> ExtractionResult:
    """Read every page of a PDF: pages with real text go through the text path, pages that
    are scans/images get rendered and go through the vision path. Injectable call_text_llm /
    call_vision default to the real backend (Groq, via llm_backends) so callers don't need to
    know which path a given PDF will take."""
    if call_text_llm is None:
        from scheduler.llm_backends import call_llm as call_text_llm
    if call_vision is None:
        from scheduler.llm_backends import call_vision_llm as call_vision

    results = []
    with pdfplumber.open(__import__("io").BytesIO(file_bytes)) as pdf:
        num_pages = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            text = (page.extract_text() or "").strip()
            if len(text) >= MIN_TEXT_CHARS:
                try:
                    results.append(extract_text_page(text, call_text_llm))
                    continue
                except ValueError:  #   -- text extraction failed; try the image instead
                    # of giving up, before falling through to the render+vision path below
                    pass
            image_bytes = _render_page(file_bytes, i)
            results.append(extract_image_page(image_bytes, call_vision))
    if num_pages == 0:
        raise ValueError("PDF has no pages")
    return _sort_result(_merge(results))


def _render_page(file_bytes: bytes, page_index: int) -> bytes:
    """One PDF page -> PNG bytes, via pypdfium2 (permissively licensed, unlike PyMuPDF)."""
    doc = pdfium.PdfDocument(file_bytes)
    try:
        page = doc.get_page(page_index)
        bitmap = page.render(scale=RENDER_SCALE)
        pil_image = bitmap.to_pil()
        buf = __import__("io").BytesIO()
        pil_image.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        doc.close()