"""Offline tests: synthetic PDFs built in-process with reportlab, fake text/vision callbacks.
No API calls, no external files."""
import io
import json
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


# ---- fix/import-partial ----

_DS = {"title": "DS", "day": "Mon", "start_time": "09:00", "end_time": "11:00"}

def test_a_text_only_pdf_never_opens_the_renderer(monkeypatch):
    def no_pdfium(*a, **kw):
        raise AssertionError("pdfium should not be opened for a text-only PDF")
    monkeypatch.setattr("scheduler.pdf_extraction.pdfium.PdfDocument", no_pdfium)
    pdf = _text_pdf(["Data Structures Monday 09:00 to 11:00 in room 4B, weekly lecture"])
    result = extract_schedule_from_pdf(pdf, call_text_llm=_fake_text("Data", "DS"), call_vision=_boom)
    assert [p.title for p in result.weekly_patterns] == ["DS"]

def test_a_pdf_the_renderer_cant_open_fails_only_the_pages_that_need_it(monkeypatch):
    def broken(*a, **kw):
        raise pdfium.PdfiumError("cannot open")
    monkeypatch.setattr("scheduler.pdf_extraction.pdfium.PdfDocument", broken)
    pdf = _multi_page_pdf([["Data Structures Monday 09:00 to 11:00 in room 4B, weekly lecture"], None,
                           ["Data Structures Monday 09:00 to 11:00 in room 4B, weekly lecture"]])
    with pytest.raises(PartialExtraction) as e:
        extract_schedule_from_pdf(pdf, call_text_llm=_fake_text("Data", "DS"), call_vision=_boom)
    assert e.value.failed_pages == [2] and len(e.value.result.weekly_patterns) == 2

def test_the_model_error_is_kept_as_the_cause_when_the_renderer_also_fails(monkeypatch):
    def broken(*a, **kw):
        raise pdfium.PdfiumError("cannot open")
    monkeypatch.setattr("scheduler.pdf_extraction.pdfium.PdfDocument", broken)
    pdf = _multi_page_pdf([["Data Structures Monday 09:00 to 11:00 in room 4B, weekly lecture"]])
    with pytest.raises(BadModelOutput):
        extract_schedule_from_pdf(pdf, call_text_llm=_wrong_format, call_vision=_boom)

@pytest.mark.parametrize("error", [pdfium.PdfiumError("bad page"), OSError("cannot encode")])
def test_one_page_that_wont_render_is_skipped_not_the_rest(monkeypatch, error):
    real_render = __import__("scheduler.pdf_extraction", fromlist=["_render_page"])._render_page
    def render(doc, i):
        if i == 0:
            raise error
        return real_render(doc, i)
    monkeypatch.setattr("scheduler.pdf_extraction._render_page", render)
    with pytest.raises(PartialExtraction) as e:
        extract_schedule_from_pdf(_multi_page_pdf([None, None]), call_text_llm=_boom, call_vision=_fake_vision("Scan"))
    assert e.value.failed_pages == [1] and len(e.value.result.weekly_patterns) == 1

@pytest.mark.parametrize("answer", [{"weekly_patterns": 3}, {"tasks": "lots of things"},
                                    {"tasks": {"0": {"title": "x"}}},
                                    {"tasks": 0}, {"tasks": False}, {"tasks": True},
                                    ["not", "an", "object"]])
def test_a_wrongly_shaped_answer_is_bad_model_output_not_a_crash(answer):
    from scheduler.schedule_extraction import extraction_from_dict
    with pytest.raises(BadModelOutput):
        extraction_from_dict(answer)

@pytest.mark.parametrize("shape", [[_DS], _DS, json.dumps([_DS])])
def test_a_list_one_item_or_a_json_string_is_read(shape):
    from scheduler.schedule_extraction import extraction_from_dict
    assert [p.title for p in extraction_from_dict({"weekly_patterns": shape}).weekly_patterns] == ["DS"]

@pytest.mark.parametrize("empty", [{}, "", None, [], "none", "N/A", "[]", "{}", [None], [""], [{}], ["N/A"], ["none"], [[]]])
def test_none_like_values_mean_no_items(empty):
    from scheduler.schedule_extraction import extraction_from_dict
    result = extraction_from_dict({"weekly_patterns": [_DS], "tasks": empty})
    assert [p.title for p in result.weekly_patterns] == ["DS"] and result.tasks == []

def test_a_page_with_a_wrongly_shaped_answer_counts_as_failed():
    pdf = _multi_page_pdf([["Data Structures Monday 09:00 to 11:00 in room 4B, weekly lecture"],
                           ["Algorithms Tuesday 10:00 to 12:00 in room 5C, weekly lecture"]])
    def text(system_prompt, user_message, tool_name, tool_schema):
        return {"weekly_patterns": [_DS]} if "Data" in user_message else {"weekly_patterns": 3}
    def vision(**kw):
        return {"weekly_patterns": 3}
    with pytest.raises(PartialExtraction) as e:
        extract_schedule_from_pdf(pdf, call_text_llm=text, call_vision=vision)
    assert e.value.failed_pages == [2] and [p.title for p in e.value.result.weekly_patterns] == ["DS"]


def test_a_wrong_shape_error_names_what_was_sent():
    from scheduler.schedule_extraction import extraction_from_dict
    with pytest.raises(BadModelOutput, match=r"\(str\)"):
        extraction_from_dict({"tasks": "true"})

def test_a_stray_non_object_in_a_list_is_dropped_not_fatal():
    from scheduler.schedule_extraction import extraction_from_dict
    assert [p.title for p in extraction_from_dict({"weekly_patterns": [_DS, "junk"]}).weekly_patterns] == ["DS"]


def test_both_causes_are_named_when_pages_fail_for_different_reasons(monkeypatch):
    def broken(*a, **kw):
        raise pdfium.PdfiumError("cannot open")
    monkeypatch.setattr("scheduler.pdf_extraction.pdfium.PdfDocument", broken)
    pdf = _multi_page_pdf([["Data Structures Monday 09:00 to 11:00 in room 4B, weekly lecture"], None,
                           ["Algorithms Tuesday 10:00 to 12:00 in room 5C, weekly lecture"]])
    def text(system_prompt, user_message, tool_name, tool_schema):
        if "Data" in user_message:
            raise BadModelOutput("didn't match the expected answer format")
        return {"weekly_patterns": [_DS]}
    with pytest.raises(PartialExtraction) as e:
        extract_schedule_from_pdf(pdf, call_text_llm=text, call_vision=_boom)
    assert e.value.failed_pages == [1, 2]
    assert "expected answer format" in str(e.value) and "PDF renderer failed" in str(e.value)

def test_a_single_titled_object_is_one_item_and_a_bad_one_is_dropped():
    from scheduler.schedule_extraction import extraction_from_dict
    good = extraction_from_dict({"tasks": {"title": "Essay", "date": "2026-10-05"}}).tasks
    assert [t.title for t in good] == ["Essay"]  # wrapped into a list of one
    assert extraction_from_dict({"tasks": {"title": "Essay"}}).tasks == []  # one bad item (no date), dropped


class _SdkError(RuntimeError):
    """Like openai.APIConnectionError: can't be rebuilt from a message alone."""
    def __init__(self, *, request=None):
        super().__init__("Connection error.")

def test_an_sdk_error_alongside_a_renderer_error_is_reported_not_a_crash(monkeypatch):
    def broken(*a, **kw):
        raise pdfium.PdfiumError("cannot open")
    monkeypatch.setattr("scheduler.pdf_extraction.pdfium.PdfDocument", broken)
    pdf = _multi_page_pdf([None, ["Data Structures Monday 09:00 to 11:00 in room 4B, weekly lecture"]])
    def text(*a, **kw):
        raise _SdkError()
    with pytest.raises(RuntimeError) as e:  # nothing was read: both reasons named, the original kept
        extract_schedule_from_pdf(pdf, call_text_llm=text, call_vision=_boom)
    assert "Connection error" in str(e.value) and "PDF renderer failed" in str(e.value)
    assert isinstance(e.value.__cause__, _SdkError)

@pytest.mark.parametrize("error", ["pdfplumber", "pdfminer"])
def test_a_page_whose_text_layer_is_broken_is_read_as_an_image(monkeypatch, error):
    from pdfminer.pdfparser import PDFSyntaxError
    from pdfplumber.utils.exceptions import PdfminerException
    import pdfplumber.page
    real = pdfplumber.page.Page.extract_text
    def extract(self, *a, **kw):
        if self.page_number == 1:  # what real pdfplumber raises for a corrupt content stream
            raise PdfminerException("broken") if error == "pdfplumber" else PDFSyntaxError("broken")
        return real(self, *a, **kw)
    monkeypatch.setattr(pdfplumber.page.Page, "extract_text", extract)
    pdf = _multi_page_pdf([["x"], ["Data Structures Monday 09:00 to 11:00 in room 4B, weekly lecture"]])
    result = extract_schedule_from_pdf(pdf, call_text_llm=_fake_text("Data", "DS"), call_vision=_fake_vision("Scan"))
    assert sorted(p.title for p in result.weekly_patterns) == ["DS", "Scan"]


def test_an_object_sent_as_text_inside_a_list_is_read():
    from scheduler.schedule_extraction import extraction_from_dict
    wed = {**_DS, "title": "B", "day": "Wed"}
    raw = {"weekly_patterns": [_DS, json.dumps(wed)]}
    assert sorted(p.title for p in extraction_from_dict(raw).weekly_patterns) == ["B", "DS"]

def test_an_empty_answer_is_still_just_empty():
    from scheduler.schedule_extraction import extraction_from_dict
    assert extraction_from_dict({}) == ExtractionResult()


def test_extra_list_or_object_fields_on_an_item_are_ignored():
    from scheduler.schedule_extraction import extraction_from_dict
    item = {**_DS, "instructors": [], "location": {"room": "B12"}}
    assert [p.title for p in extraction_from_dict({"weekly_patterns": [item]}).weekly_patterns] == ["DS"]
    assert [p.title for p in extraction_from_dict({"weekly_patterns": item}).weekly_patterns] == ["DS"]

def test_an_unknown_text_note_beside_the_answer_is_fine():
    from scheduler.schedule_extraction import extraction_from_dict
    assert extraction_from_dict({"weekly_patterns": [_DS], "note": "all weekly"}).weekly_patterns

def _raising_from(filename, error):
    """A function whose code says it lives in `filename`, so the error looks raised there."""
    ns = {"error": error}
    exec(compile("def extract(self, *a, **kw):\n    raise error\n", filename, "exec"), ns)
    return ns["extract"]

def test_any_error_reading_a_text_layer_sends_the_page_to_the_image_path(monkeypatch):
    import pdfplumber.page
    monkeypatch.setattr(pdfplumber.page.Page, "extract_text",
                        _raising_from("/site-packages/pdfminer/pdffont.py", KeyError("corrupt font")))
    result = extract_schedule_from_pdf(_multi_page_pdf([["x"]]), call_text_llm=_boom, call_vision=_fake_vision("Scan"))
    assert [p.title for p in result.weekly_patterns] == ["Scan"]


def test_a_task_sent_as_json_text_gets_the_same_due_time_handling():
    import json
    from scheduler.schedule_extraction import extraction_from_dict
    task = {"title": "HW", "date": "2026-10-05", "due_time": "TBD"}
    as_object = extraction_from_dict({"tasks": [task]}).tasks
    as_text = extraction_from_dict({"tasks": [json.dumps(task)]}).tasks
    assert [(t.title, t.due_time) for t in as_text] == [(t.title, t.due_time) for t in as_object] == [("HW", None)]


# ---- one door for any document: images go to the vision path, PDFs page by page ----
from scheduler import pdf_extraction


def test_page_count_reads_the_pdf_once_without_any_call():
    assert pdf_extraction.page_count(_multi_page_pdf([["one"], ["two"], None])) == 3


def test_extract_document_routes_pdfs_to_the_page_reader_and_images_to_vision(monkeypatch):
    seen = []
    monkeypatch.setattr(pdf_extraction, "extract_schedule_from_pdf", lambda b: seen.append(("pdf", b)) or ExtractionResult())
    monkeypatch.setattr(pdf_extraction, "extract_schedule", lambda b, m: seen.append((m, b)) or ExtractionResult())
    pdf_extraction.extract_document(b"%PDF", "application/pdf")
    pdf_extraction.extract_document(b"png", "image/png")
    assert seen == [("pdf", b"%PDF"), ("image/png", b"png")]
