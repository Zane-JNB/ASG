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

import base64
import io

import pdfplumber
import pypdfium2 as pdfium

from scheduler.llm_backends import BadModelOutput, is_backend_failure
from scheduler.models import ExtractionResult
from scheduler.schedule_extraction import _sort_result, build_extraction_system_prompt, extraction_from_dict

MIN_TEXT_CHARS = 40  # below this, treat the page as "no usable text" and render it instead
RENDER_SCALE = 2.0   # ~144 DPI; enough to read timetable text without huge file sizes


class RenderError(RuntimeError):
    """A page couldn't be rendered (or pdfium couldn't open the PDF): only that page fails."""


class PartialExtraction(RuntimeError):
    """Some pages were read and some failed. .result holds what the read pages gave (already
    paid for); .failed_pages lists the 1-based page numbers that failed or were never tried."""
    def __init__(self, result: ExtractionResult, failed_pages: list[int], cause: Exception, note: str = ""):
        pages = ", ".join(map(str, failed_pages))
        super().__init__(f"page(s) {pages} could not be read: {cause}{note}")
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
        image_base64=base64.standard_b64encode(image_bytes).decode("utf-8"),
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
    would fail too). The renderer is opened only when a page needs it; if it can't open the
    PDF or render a page, only the pages that needed it fail. If some pages were read,
    PartialExtraction carries them; if none were, the error itself is raised."""
    if call_text_llm is None:
        from scheduler.llm_backends import call_llm as call_text_llm
    if call_vision is None:
        from scheduler.llm_backends import call_vision_llm as call_vision

    results, failed = [], []
    model_cause = render_cause = None  # a model error says more than a renderer error; name both
    renderer = _LazyRenderer(file_bytes)
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            num_pages = len(pdf.pages)
            for i, page in enumerate(pdf.pages):
                text_error = None
                try:
                    try:
                        text = (page.extract_text() or "").strip()
                    except Exception:  # a direct library call: a broken text layer (pdfminer raises many kinds)
                        text = ""      # is read as an image instead
                    if len(text) >= MIN_TEXT_CHARS:
                        try:
                            results.append(extract_text_page(text, call_text_llm))
                            continue
                        except BadModelOutput as e:  # wrong format: try the page as an image instead
                            text_error = e
                    results.append(extract_image_page(renderer.render(i), call_vision))
                except Exception as e:
                    if not is_backend_failure(e):
                        raise  # a bug in our own code: keep the traceback
                    failed.append(i + 1)
                    if isinstance(e, RenderError):
                        render_cause = e
                        model_cause = text_error or model_cause
                    else:
                        model_cause = e
                    if not isinstance(e, (BadModelOutput, RenderError)):  # rate limit, key, model: stop now
                        failed += range(i + 2, num_pages + 1)
                        break
    finally:
        renderer.close()
    if num_pages == 0:
        raise ValueError("PDF has no pages")
    if failed:
        cause = model_cause or render_cause
        note = f" (and the PDF renderer failed: {render_cause})" if model_cause and render_cause else ""
        if not results:
            if note:  # name both; a new error, since an SDK error can't be rebuilt from a message
                raise (BadModelOutput if isinstance(cause, BadModelOutput) else RuntimeError)(f"{cause}{note}") from cause
            raise cause
        raise PartialExtraction(_sort_result(_merge(results)), failed, cause, note)
    return _sort_result(_merge(results))


class _LazyRenderer:
    """Renders pages as PNG bytes, opening the document on first use so a text-only PDF never
    needs pdfium. A document pdfium can't open (tried once), or a page that won't render or
    encode, raises RenderError, which fails only that page."""
    def __init__(self, file_bytes: bytes):
        self._bytes, self._doc, self._error = file_bytes, None, None

    def render(self, page_index: int) -> bytes:
        if self._doc is None and self._error is None:
            try:
                self._doc = pdfium.PdfDocument(self._bytes)
            except pdfium.PdfiumError as e:
                self._error = e
        if self._error is not None:
            raise RenderError(f"the PDF renderer couldn't open this file: {self._error}")
        try:
            return _render_page(self._doc, page_index)
        except Exception as e:  # _render_page is only pdfium/Pillow calls: any failure is this page's
            raise RenderError(f"page {page_index + 1} couldn't be rendered: {e}") from e

    def close(self) -> None:
        if self._doc is not None:
            self._doc.close()


def _render_page(doc, page_index: int) -> bytes:
    """One page of an open pypdfium2 PdfDocument -> PNG bytes (pypdfium2 is permissively
    licensed, unlike PyMuPDF)."""
    page = doc.get_page(page_index)
    bitmap = page.render(scale=RENDER_SCALE)
    buf = io.BytesIO()
    bitmap.to_pil().save(buf, format="PNG")
    return buf.getvalue()
