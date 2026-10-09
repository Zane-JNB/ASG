"""Live Groq checks: 3 real calls, skipped without GROQ_API_KEY. Not part of the default run's
offline guarantee -- run them on purpose with `pytest -m live`. They check the shape of the
answer only, never exact content (model output varies). Inputs are synthetic."""
import io
import time

import pytest

from scheduler.models import ExtractionResult
from scheduler.pdf_extraction import extract_schedule_from_pdf
from scheduler.reflection import ReflectionResult
from scheduler.reflection_cycle import get_proposals
from scheduler.schedule_extraction import extract_schedule

pytestmark = pytest.mark.live

# Groq's free tier also caps tokens per minute per model, and the extraction prompt is long, so
# back-to-back calls can hit a 429 even far below 30 requests/minute. Space the calls out.
GAP_SECONDS = 10


@pytest.fixture(autouse=True)
def space_out_live_calls():
    yield
    time.sleep(GAP_SECONDS)

TIMETABLE_LINES = ["Weekly Schedule", "Monday 09:00-11:00 Data Structures",
                   "Wednesday 14:00-15:30 Linear Algebra"]


def test_live_reflection_returns_a_reflection_result():
    result = get_proposals("This week felt really rushed -- no breaks between class and studying.")
    assert isinstance(result, ReflectionResult)


def test_live_image_extraction_returns_an_extraction_result():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (640, 200), "white")
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(TIMETABLE_LINES):
        draw.text((20, 20 + 40 * i), line, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    assert isinstance(extract_schedule(buf.getvalue(), "image/png"), ExtractionResult)


def test_live_pdf_text_path_returns_an_extraction_result():
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for i, line in enumerate(TIMETABLE_LINES):
        c.drawString(72, 700 - 20 * i, line)
    c.save()
    assert isinstance(extract_schedule_from_pdf(buf.getvalue()), ExtractionResult)
