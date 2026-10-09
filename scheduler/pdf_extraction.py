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

from scheduler.llm_backends import BadModelOutput, is_backend_failure
from scheduler.models import ExtractionResult
from scheduler.schedule_extraction import _sort_result, build_extraction_system_prompt, extraction_from_dict

MIN_TEXT_CHARS = 40  # below this, treat the page as "no usable text" and render it instead
RENDER_SCALE = 2.0   # ~144 DPI; enough to read timetable text without huge file sizes


class PartialExtraction(RuntimeError):
    """Some pages were read and some failed. .result holds what the read pages gave (already
    paid for); .failed_pages lists the 1-based page numbers that failed or were never tried."""
    def __init__(self, result: ExtractionResult, failed_pages: list[int], cause: Exception):
        pages = ", ".join(map(str, failed_pages))
        super().__init__(f"page(s) {pages} could not be read: {cause}")
        self.result, self.failed_pages, self.cause = result, failed_pages, cause


def _merge(results: list[ExtractionResult]) -> ExtractionResult:
    return ExtractionResult(
        weekly_patterns=[p for r in results for p in r.weekly_patterns],
        dated_blocks=[b for r in results for b in r.dated_blocks],
        tasks=[t for r in results for t in r.tasks],
    )

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
    return extraction_from_dict(raw)


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
    return extraction_from_dict(raw)


def extract_schedule_from_pdf(file_bytes: bytes, call_text_llm=None, call_vision=None) -> ExtractionResult:
    """Read every page of a PDF: pages with real text go through the text path, pages that
    are scans/images get rendered and go through the vision path. Injectable call_text_llm /
    call_vision default to the real backend (Groq, via llm_backends) so callers don't need to
    know which path a given PDF will take.

    A text page whose answer comes back in the wrong format is retried as an image. A page
    that still fails is skipped; a rate limit, key or model error stops at once (more calls
    would fail too). If some pages were read, PartialExtraction carries them; if none were,
    the error itself is raised."""
    if call_text_llm is None:
        from scheduler.llm_backends import call_llm as call_text_llm
    if call_vision is None:
        from scheduler.llm_backends import call_vision_llm as call_vision

    results, failed, cause = [], [], None
    doc = pdfium.PdfDocument(file_bytes)  # opened once, only rendered from if a page needs it
    try:
        with pdfplumber.open(__import__("io").BytesIO(file_bytes)) as pdf:
            num_pages = len(pdf.pages)
            for i, page in enumerate(pdf.pages):
                text = (page.extract_text() or "").strip()
                try:
                    if len(text) >= MIN_TEXT_CHARS:
                        try:
                            results.append(extract_text_page(text, call_text_llm))
                            continue
                        except BadModelOutput:  # wrong format: try the page as an image instead
                            pass
                    results.append(extract_image_page(_render_page(doc, i), call_vision))
                except Exception as e:
                    if not is_backend_failure(e):
                        raise  # a bug in our own code: keep the traceback
                    failed.append(i + 1)
                    cause = e
                    if not isinstance(e, BadModelOutput):  # rate limit, key, model: stop now
                        failed += range(i + 2, num_pages + 1)
                        break
    finally:
        doc.close()
    if num_pages == 0:
        raise ValueError("PDF has no pages")
    if failed:
        if not results:
            raise cause
        raise PartialExtraction(_sort_result(_merge(results)), failed, cause)
    return _sort_result(_merge(results))


def _render_page(doc, page_index: int) -> bytes:
    """One page of an open pypdfium2 PdfDocument -> PNG bytes (pypdfium2 is permissively
    licensed, unlike PyMuPDF)."""
    page = doc.get_page(page_index)
    bitmap = page.render(scale=RENDER_SCALE)
    buf = __import__("io").BytesIO()
    bitmap.to_pil().save(buf, format="PNG")
    return buf.getvalue()
