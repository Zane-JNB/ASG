import pytest

from scheduler.db import (connect, get_or_create_student, replace_extraction,
                          get_weekly_patterns, get_dated_blocks, get_extracted_tasks)
from scheduler.import_flow import run_import
from scheduler.models import WeeklyPattern, DatedBlock, ExtractedTask, ExtractionResult


def _extraction(title="DS"):
    return ExtractionResult(
        weekly_patterns=[WeeklyPattern(title=title, day="Mon", start_time="09:00", end_time="11:00")],
        dated_blocks=[DatedBlock(title="Lab", date="2026-10-01", start_time="10:00", end_time="12:00")],
        tasks=[ExtractedTask(title="HW", date="2026-10-02")],
    )


KEEP_ALL = [""]  # NEW -- one review prompt: Enter accepts everything


class Recorder:
    """Scripted stand-in for input() that also remembers every prompt it was shown."""
    def __init__(self, answers):
        self.answers, self.prompts = iter(answers), []
    def __call__(self, prompt):
        self.prompts.append(prompt)
        return next(self.answers)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)  # default = fake, no cost prompt
    conn = connect(":memory:")
    sid = get_or_create_student(conn, "Z")
    img = tmp_path / "t.png"
    img.write_bytes(b"fake-image-bytes")
    return conn, sid, img, str(tmp_path / "cache.json")


def _titles(conn, sid):
    return ([p.title for _, p in get_weekly_patterns(conn, sid)],
            [b.title for _, b in get_dated_blocks(conn, sid)],
            [t.title for _, t in get_extracted_tasks(conn, sid)])


def test_image_flow_extracts_reviews_saves_and_caches(env):
    conn, sid, img, cache = env
    calls = []
    def extractor(data, media_type):
        calls.append((data, media_type))
        return _extraction()
    ok = run_import(conn, sid, str(img), ask=Recorder(KEEP_ALL), show=lambda _: None,
                    extractor=extractor, cache_path=cache)
    assert ok and calls == [(b"fake-image-bytes", "image/png")]
    assert _titles(conn, sid) == (["DS"], ["Lab"], ["HW"])
    assert ExtractionResult.model_validate_json(open(cache, encoding="utf-8").read()) == _extraction()


def test_json_replay_makes_no_extractor_call(env, tmp_path):
    conn, sid, _, cache = env
    saved = tmp_path / "saved.json"
    saved.write_text(_extraction("Replay").model_dump_json(), encoding="utf-8")
    def boom(*a):
        raise AssertionError("extractor must not be called on replay")
    ask = Recorder(KEEP_ALL)
    assert run_import(conn, sid, str(saved), ask=ask, show=lambda _: None, extractor=boom, cache_path=cache)
    assert _titles(conn, sid)[0] == ["Replay"]


def test_real_backend_asks_cost_first_and_declining_calls_nothing(env, monkeypatch):
    conn, sid, img, cache = env
    monkeypatch.setenv("LLM_BACKEND", "groq")
    def boom(*a):
        raise AssertionError("must not be called after declining")
    ask = Recorder([""])  # Enter = default No
    assert not run_import(conn, sid, str(img), ask=ask, show=lambda _: None, extractor=boom, cache_path=cache)
    assert "free-tier" in ask.prompts[0]
    assert _titles(conn, sid) == ([], [], [])


def test_paid_backend_is_labelled_paid(env, monkeypatch):
    conn, sid, img, cache = env
    monkeypatch.setenv("LLM_BACKEND", "anthropic")
    ask = Recorder(["n"])
    run_import(conn, sid, str(img), ask=ask, show=lambda _: None, cache_path=cache)
    assert "paid" in ask.prompts[0]


def test_fake_backend_skips_cost_prompt(env):
    conn, sid, img, cache = env
    ask = Recorder(KEEP_ALL)
    run_import(conn, sid, str(img), ask=ask, show=lambda _: None,
               extractor=lambda d, m: _extraction(), cache_path=cache)
    assert all("LLM_BACKEND" not in p for p in ask.prompts)


def test_empty_extraction_leaves_saved_data_alone(env):
    conn, sid, img, cache = env
    replace_extraction(conn, sid, _extraction("Keep"))
    shown = []
    ok = run_import(conn, sid, str(img), ask=Recorder([]), show=shown.append,
                    extractor=lambda d, m: ExtractionResult(), cache_path=cache)
    assert not ok and _titles(conn, sid)[0] == ["Keep"]
    assert any("untouched" in s for s in shown)


def test_rejecting_everything_leaves_saved_data_alone(env):
    conn, sid, img, cache = env
    replace_extraction(conn, sid, _extraction("Keep"))
    ok = run_import(conn, sid, str(img), ask=Recorder(["1 2 3", "d", "d", "d"]), show=lambda _: None,
                    extractor=lambda d, m: _extraction("New"), cache_path=cache)
    assert not ok and _titles(conn, sid)[0] == ["Keep"]


def test_reupload_replaces_old_items(env):
    conn, sid, img, cache = env
    replace_extraction(conn, sid, _extraction("Old"))
    run_import(conn, sid, str(img), ask=Recorder(KEEP_ALL), show=lambda _: None,
               extractor=lambda d, m: _extraction("New"), cache_path=cache)
    assert _titles(conn, sid) == (["New"], ["Lab"], ["HW"])


def test_unsupported_extension(env, tmp_path):
    conn, sid, _, cache = env
    bad = tmp_path / "t.txt"; bad.write_text("x")
    shown = []
    assert not run_import(conn, sid, str(bad), ask=Recorder([]), show=shown.append, cache_path=cache)
    assert any("Unsupported" in s for s in shown)


def test_extractor_value_error_is_reported_not_raised(env, tmp_path):
    conn, sid, _, cache = env
    pdf = tmp_path / "t.pdf"; pdf.write_bytes(b"%PDF")
    def groq_style(data, media_type):
        raise ValueError("Groq's vision model doesn't support PDF input")
    shown = []
    assert not run_import(conn, sid, str(pdf), ask=Recorder([]), show=shown.append,
                          extractor=groq_style, cache_path=cache)
    assert any("Extraction failed" in s for s in shown)

def test_summary_message_says_what_was_replaced_and_what_was_added(env):
    conn, sid, img, cache = env
    shown = []
    run_import(conn, sid, str(img), ask=Recorder(KEEP_ALL), show=shown.append,
               extractor=lambda d, m: _extraction(), cache_path=cache)
    assert shown[-1] == "Saved: 1 weekly (replaced old), 1 dated (replaced old), 1 task(s) added."
    shown.clear()  # same import again: tasks now repeat
    run_import(conn, sid, str(img), ask=Recorder(KEEP_ALL), show=shown.append,
               extractor=lambda d, m: _extraction(), cache_path=cache)
    assert shown[-1] == ("Saved: 1 weekly (replaced old), 1 dated (replaced old), "
                         "0 task(s) added, 1 duplicate(s) skipped.")

def test_pdf_uses_the_local_pdf_path_not_the_default_extractor(env, monkeypatch, tmp_path):
    """A .pdf must NOT go through extract_schedule (which would reach the vision backend
    and reject PDFs on Groq) -- it should route through extract_schedule_from_pdf instead."""
    conn, sid, _, cache = env
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    import io
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.drawString(72, 700, "Weekly Schedule")
    c.drawString(72, 680, "Monday 09:00-11:00 Data Structures")
    c.save()
    pdf_path = tmp_path / "t.pdf"
    pdf_path.write_bytes(buf.getvalue())

    def fake_from_pdf(file_bytes, call_text_llm=None, call_vision=None):
        return _extraction("PDF-DS")

    monkeypatch.setattr("scheduler.import_flow.extract_schedule_from_pdf", fake_from_pdf)
    ask = Recorder(KEEP_ALL)
    ok = run_import(conn, sid, str(pdf_path), ask=ask, show=lambda _: None, cache_path=cache)
    assert ok and _titles(conn, sid)[0] == ["PDF-DS"]
    assert all("LLM_BACKEND" not in p for p in ask.prompts)  # no vision cost prompt for the PDF path

def test_pdf_cost_prompt_mentions_page_count(env, monkeypatch, tmp_path):
    conn, sid, _, cache = env
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    import io
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.drawString(72, 700, "page one"); c.showPage()
    c.drawString(72, 700, "page two"); c.showPage()
    c.save()
    pdf_path = tmp_path / "two_pages.pdf"
    pdf_path.write_bytes(buf.getvalue())

    monkeypatch.setenv("LLM_BACKEND", "groq")
    ask = Recorder([""])  # decline (Enter = default No)
    run_import(conn, sid, str(pdf_path), ask=ask, show=lambda _: None, cache_path=cache)
    assert "2 page(s)" in ask.prompts[0]