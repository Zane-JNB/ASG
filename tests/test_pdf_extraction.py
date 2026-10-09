"""Offline tests: synthetic PDFs built in-process with reportlab, fake text/vision callbacks.
No API calls, no external files."""
import io
import pytest

from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

import pypdfium2 as pdfium

from scheduler.llm_backends import BadModelOutput
from scheduler.pdf_extraction import PartialExtraction, extract_schedule_from_pdf, extract_text_page, _render_page
from scheduler.models import ExtractionResult


def _text_pdf(lines: list[str]) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for i, line in enumerate(lines):
        c.drawString(72, 700 - i * 20, line)
    c.save()
    return buf.getvalue()


def _image_only_pdf() -> bytes:
    """A page with a drawn image and no real text layer -- simulates a scanned page."""
    from PIL import Image
    img_buf = io.BytesIO()
    Image.new("RGB", (200, 80), "white").save(img_buf, format="PNG")
    img_buf.seek(0)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.drawImage(ImageReader(img_buf), 72, 600, width=200, height=80)
    c.save()
    return buf.getvalue()


def _multi_page_pdf(page_texts: list) -> bytes:
    """None entries become image-only pages."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    from PIL import Image
    for lines in page_texts:
        if lines is None:
            img_buf = io.BytesIO()
            Image.new("RGB", (200, 80), "white").save(img_buf, format="PNG")
            img_buf.seek(0)
            c.drawImage(ImageReader(img_buf), 72, 600, width=200, height=80)
        else:
            for i, line in enumerate(lines):
                c.drawString(72, 700 - i * 20, line)
        c.showPage()
    c.save()
    return buf.getvalue()


def _fake_text(pattern_word: str, title: str):
    def call(system_prompt, user_message, tool_name, tool_schema):
        if pattern_word in user_message:
            return {"weekly_patterns": [{"title": title, "day": "Mon", "start_time": "09:00", "end_time": "11:00"}]}
        return {"weekly_patterns": [], "dated_blocks": [], "tasks": []}
    return call


def _fake_vision(title: str):
    def call(system_prompt, user_text, image_base64, media_type, tool_name, tool_schema):
        return {"weekly_patterns": [{"title": title, "day": "Wed", "start_time": "10:00", "end_time": "12:00"}]}
    return call


def _boom(*a, **kw):
    raise AssertionError("this path should not have been called")


def test_text_page_uses_the_text_path_not_vision():
    pdf = _text_pdf(["Weekly Schedule", "Monday 09:00-11:00 Data Structures",
                     "Tuesday 13:00-14:00 Algorithms"])
    result = extract_schedule_from_pdf(pdf, call_text_llm=_fake_text("Monday", "DS"), call_vision=_boom)
    assert [p.title for p in result.weekly_patterns] == ["DS"]


def test_scanned_page_falls_back_to_the_vision_path():
    pdf = _image_only_pdf()
    result = extract_schedule_from_pdf(pdf, call_text_llm=_boom, call_vision=_fake_vision("Scanned Lab"))
    assert [p.title for p in result.weekly_patterns] == ["Scanned Lab"]


def test_text_with_no_schedule_content_returns_empty_without_crashing():
    pdf = _text_pdf(["This document has nothing to do with a schedule.",
                     "It is just a page of unrelated prose about something else entirely."])
    result = extract_schedule_from_pdf(pdf, call_text_llm=_fake_text("Monday", "DS"), call_vision=_boom)
    assert result == ExtractionResult()


def test_mixed_pdf_routes_each_page_independently_and_merges_results():
    pdf = _multi_page_pdf([
        ["Weekly Schedule", "Monday 09:00-11:00 Data Structures"], None,
        ["Some unrelated page of prose with no schedule information on it at all."]])
    result = extract_schedule_from_pdf(
        pdf, call_text_llm=_fake_text("Monday", "DS"), call_vision=_fake_vision("Scanned Lab"))
    assert sorted(p.title for p in result.weekly_patterns) == ["DS", "Scanned Lab"]


def test_extract_text_page_directly():
    result = extract_text_page("Monday 09:00-11:00 DS", _fake_text("Monday", "DS"))
    assert [p.title for p in result.weekly_patterns] == ["DS"]


def test_render_page_produces_a_real_png():
    doc = pdfium.PdfDocument(_image_only_pdf())
    try:
        png_bytes = _render_page(doc, 0)
    finally:
        doc.close()
    assert png_bytes.startswith(b"\x89PNG")


def test_a_malformed_item_from_the_model_is_dropped_not_fatal():
    def bad_text(system_prompt, user_message, tool_name, tool_schema):
        return {"weekly_patterns": [
            {"title": "Good", "day": "Mon", "start_time": "09:00", "end_time": "11:00"},
            {"title": "Bad", "day": "Notaday", "start_time": "09:00", "end_time": "11:00"},  # invalid day
        ]}
    pdf = _text_pdf(["Weekly Schedule", "Monday 09:00-11:00 Good class here"])
    result = extract_schedule_from_pdf(pdf, call_text_llm=bad_text, call_vision=_boom)
    assert [p.title for p in result.weekly_patterns] == ["Good"]

# ---- a failed text-path call must fall back to rendering + vision, not silently return nothing ----
def _wrong_format(*a, **kw):
    """What call_llm raises when Groq's tool call doesn't match the schema (tool_use_failed)."""
    raise BadModelOutput("didn't match the expected answer format")


def _rate_limited(*a, **kw):
    raise RuntimeError("Groq's free-tier rate limit was hit.")


def test_failed_text_call_falls_back_to_vision_instead_of_reporting_nothing_found():
    pdf = _text_pdf(["Weekly Schedule", "Monday 09:00-11:00 Data Structures",
                     "Tuesday 13:00-14:00 Algorithms"])
    result = extract_schedule_from_pdf(
        pdf, call_text_llm=_wrong_format, call_vision=_fake_vision("Rescued via vision"))
    assert [p.title for p in result.weekly_patterns] == ["Rescued via vision"]


def test_extract_text_page_raises_on_wrong_format():
    with pytest.raises(BadModelOutput):
        extract_text_page("Monday 09:00-11:00 DS", _wrong_format)


_THREE_TEXT_PAGES = [["Weekly Schedule", "Monday 09:00-11:00 Data Structures"]] * 3


def _text_then(fail_from: int, error):
    """Text callback that reads pages fine until call number `fail_from` (1-based), then fails."""
    calls = []
    def call(system_prompt, user_message, tool_name, tool_schema):
        calls.append(1)
        if len(calls) >= fail_from:
            error()
        return {"weekly_patterns": [{"title": f"P{len(calls)}", "day": "Mon", "start_time": "09:00", "end_time": "11:00"}]}
    return call


def test_rate_limit_mid_pdf_keeps_the_pages_already_read_and_stops():
    pdf = _multi_page_pdf(_THREE_TEXT_PAGES)
    with pytest.raises(PartialExtraction) as info:
        extract_schedule_from_pdf(pdf, call_text_llm=_text_then(2, lambda: _rate_limited()), call_vision=_boom)
    assert [p.title for p in info.value.result.weekly_patterns] == ["P1"]
    assert info.value.failed_pages == [2, 3]  # page 3 was never tried: no extra calls


def test_a_page_that_fails_both_ways_is_skipped_and_the_rest_still_read():
    pdf = _multi_page_pdf(_THREE_TEXT_PAGES)
    def text(system_prompt, user_message, tool_name, tool_schema):
        text.n = getattr(text, "n", 0) + 1
        if text.n == 2:
            raise BadModelOutput("wrong format")
        return {"weekly_patterns": [{"title": f"P{text.n}", "day": "Mon", "start_time": "09:00", "end_time": "11:00"}]}
    with pytest.raises(PartialExtraction) as info:
        extract_schedule_from_pdf(pdf, call_text_llm=text, call_vision=_wrong_format)
    assert sorted(p.title for p in info.value.result.weekly_patterns) == ["P1", "P3"]
    assert info.value.failed_pages == [2]


def test_nothing_read_at_all_raises_the_error_itself():
    pdf = _multi_page_pdf(_THREE_TEXT_PAGES)
    with pytest.raises(RuntimeError, match="rate limit") as info:
        extract_schedule_from_pdf(pdf, call_text_llm=_rate_limited, call_vision=_boom)
    assert not isinstance(info.value, PartialExtraction)


def test_a_bug_in_our_own_code_is_not_hidden_as_a_failed_page():
    pdf = _text_pdf(["Weekly Schedule", "Monday 09:00-11:00 Data Structures"])
    def buggy(*a, **kw):
        raise KeyError("oops")
    with pytest.raises(KeyError):
        extract_schedule_from_pdf(pdf, call_text_llm=buggy, call_vision=_boom)


def test_non_object_items_from_the_model_are_dropped():
    def odd(system_prompt, user_message, tool_name, tool_schema):
        return {"weekly_patterns": ["not an object", {"title": "Good", "day": "Mon", "start_time": "09:00", "end_time": "11:00"}],
                "tasks": None}
    pdf = _text_pdf(["Weekly Schedule", "Monday 09:00-11:00 Good class here"])
    result = extract_schedule_from_pdf(pdf, call_text_llm=odd, call_vision=_boom)
    assert [p.title for p in result.weekly_patterns] == ["Good"]
