"""Offline tests: synthetic PDFs built in-process with reportlab, fake text/vision callbacks.
No API calls, no external files."""
import io
import pytest

from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from scheduler.pdf_extraction import extract_schedule_from_pdf, extract_text_page, _render_page
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
    png_bytes = _render_page(_image_only_pdf(), 0)
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
def _reflection_shaped_fallback(system_prompt, user_message, tool_name, tool_schema):
    """Mimics what _groq_call returns when Groq's tool_use_failed soft-degrade fires --
    shaped for reflections (summary/proposals), NOT for extraction."""
    return {"summary": "didn't match expected format", "proposals": []}


def test_failed_text_call_falls_back_to_vision_instead_of_reporting_nothing_found():
    pdf = _text_pdf(["Weekly Schedule", "Monday 09:00-11:00 Data Structures",
                     "Tuesday 13:00-14:00 Algorithms"])
    result = extract_schedule_from_pdf(
        pdf, call_text_llm=_reflection_shaped_fallback, call_vision=_fake_vision("Rescued via vision"))
    assert [p.title for p in result.weekly_patterns] == ["Rescued via vision"]


def test_extract_text_page_raises_on_reflection_shaped_fallback():
    with pytest.raises(ValueError):
        extract_text_page("Monday 09:00-11:00 DS", _reflection_shaped_fallback)