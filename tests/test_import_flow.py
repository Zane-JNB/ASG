from datetime import datetime

import pytest

from scheduler.db import (connect, get_or_create_student, replace_extraction,
                          get_weekly_patterns, get_dated_blocks, get_extracted_tasks)
from scheduler.import_flow import run_import
from scheduler.models import WeeklyPattern, DatedBlock, ExtractedTask, ExtractionResult


@pytest.fixture(autouse=True)
def _import_clock_before_the_sample_dates(monkeypatch):
    """run_import asks about tasks already due; these samples are dated Oct 2026, so pin 'now' before them."""
    class _Before(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 1, 9, 0)
    monkeypatch.setattr("scheduler.import_flow.datetime", _Before)


def _extraction(title="DS"):
    return ExtractionResult(
        weekly_patterns=[WeeklyPattern(title=title, day="Mon", start_time="09:00", end_time="11:00")],
        dated_blocks=[DatedBlock(title="Lab", date="2026-10-01", start_time="10:00", end_time="12:00")],
        tasks=[ExtractedTask(title="HW", date="2026-10-02")],
    )


KEEP_ALL = [""]  #   -- one review prompt: Enter accepts everything
YES = ["y"]      #   -- the Groq cost prompt every image/PDF import asks first


class Recorder:
    """Scripted stand-in for input() that also remembers every prompt it was shown."""
    def __init__(self, answers):
        self.answers, self.prompts = iter(answers), []
    def __call__(self, prompt):
        self.prompts.append(prompt)
        return next(self.answers)


@pytest.fixture
def env(tmp_path, monkeypatch):
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
    ok = run_import(conn, sid, str(img), ask=Recorder(YES + KEEP_ALL), show=lambda _: None,
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


def test_asks_groq_cost_first_and_declining_calls_nothing(env):
    conn, sid, img, cache = env
    def boom(*a):
        raise AssertionError("must not be called after declining")
    ask = Recorder([""])  # Enter = default No
    assert not run_import(conn, sid, str(img), ask=ask, show=lambda _: None, extractor=boom, cache_path=cache)
    assert "Groq (free tier, but a real API call)" in ask.prompts[0]
    assert _titles(conn, sid) == ([], [], [])


def test_empty_extraction_leaves_saved_data_alone(env):
    conn, sid, img, cache = env
    replace_extraction(conn, sid, _extraction("Keep"))
    shown = []
    ok = run_import(conn, sid, str(img), ask=Recorder(YES), show=shown.append,
                    extractor=lambda d, m: ExtractionResult(), cache_path=cache)
    assert not ok and _titles(conn, sid)[0] == ["Keep"]
    assert any("untouched" in s for s in shown)


def test_rejecting_everything_leaves_saved_data_alone(env):
    conn, sid, img, cache = env
    replace_extraction(conn, sid, _extraction("Keep"))
    ok = run_import(conn, sid, str(img), ask=Recorder(YES + ["1 2 3", "d", "d", "d"]), show=lambda _: None,
                    extractor=lambda d, m: _extraction("New"), cache_path=cache)
    assert not ok and _titles(conn, sid)[0] == ["Keep"]


def test_reupload_replaces_old_items(env):
    conn, sid, img, cache = env
    replace_extraction(conn, sid, _extraction("Old"))
    run_import(conn, sid, str(img), ask=Recorder(YES + KEEP_ALL), show=lambda _: None,
               extractor=lambda d, m: _extraction("New"), cache_path=cache)
    assert _titles(conn, sid) == (["New"], ["Lab"], ["HW"])


def test_unsupported_extension(env, tmp_path):
    conn, sid, _, cache = env
    bad = tmp_path / "t.txt"; bad.write_text("x")
    shown = []
    assert not run_import(conn, sid, str(bad), ask=Recorder([]), show=shown.append, cache_path=cache)
    assert any("Unsupported" in s for s in shown)


def test_extractor_value_error_is_reported_not_raised(env):
    conn, sid, img, cache = env
    def groq_style(data, media_type):
        raise ValueError("Groq's vision model doesn't support PDF input")
    shown = []
    assert not run_import(conn, sid, str(img), ask=Recorder(YES), show=shown.append,
                          extractor=groq_style, cache_path=cache)
    assert any("Extraction failed" in s for s in shown)

@pytest.mark.parametrize("name", ["missing.json", "missing.png"])
def test_missing_file_is_reported_not_raised(env, tmp_path, name):
    conn, sid, _, cache = env
    shown = []
    assert not run_import(conn, sid, str(tmp_path / name), ask=Recorder([]), show=shown.append,
                          cache_path=cache)
    assert any("Could not read" in s for s in shown)


@pytest.mark.parametrize("text", ["{not json", '{"weekly_patterns": [{"title": 5}]}'])
def test_bad_json_replay_is_reported_not_raised(env, tmp_path, text):
    conn, sid, _, cache = env
    bad = tmp_path / "bad.json"; bad.write_text(text, encoding="utf-8")
    shown = []
    assert not run_import(conn, sid, str(bad), ask=Recorder([]), show=shown.append, cache_path=cache)
    assert any("not a valid saved extraction" in s for s in shown)


def test_extractor_runtime_error_is_reported_not_raised(env):
    conn, sid, img, cache = env
    def groq_style(data, media_type):
        raise RuntimeError("Groq model was decommissioned")
    shown = []
    assert not run_import(conn, sid, str(img), ask=Recorder(YES), show=shown.append,
                          extractor=groq_style, cache_path=cache)
    assert any("Extraction failed" in s for s in shown)


def test_summary_message_says_what_was_replaced_and_what_was_added(env):
    conn, sid, img, cache = env
    shown = []
    run_import(conn, sid, str(img), ask=Recorder(YES + KEEP_ALL), show=shown.append,
               extractor=lambda d, m: _extraction(), cache_path=cache)
    assert shown[-1] == "Saved: 1 weekly (replaced old), 1 dated (replaced old), 1 task(s) added."
    shown.clear()  # same import again: tasks now repeat
    run_import(conn, sid, str(img), ask=Recorder(YES + KEEP_ALL), show=shown.append,
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
    ask = Recorder(YES + KEEP_ALL)
    ok = run_import(conn, sid, str(pdf_path), ask=ask, show=lambda _: None, cache_path=cache)
    assert ok and _titles(conn, sid)[0] == ["PDF-DS"]

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

    ask = Recorder([""])  # decline (Enter = default No)
    run_import(conn, sid, str(pdf_path), ask=ask, show=lambda _: None, cache_path=cache)
    assert "2 page(s)" in ask.prompts[0]

def test_data_files_live_in_the_repo_root_whatever_the_cwd():
    import os
    from scheduler import import_flow, paths
    root = os.path.dirname(os.path.dirname(os.path.abspath(import_flow.__file__)))
    assert import_flow.CACHE_PATH == paths.CACHE_PATH == os.path.join(root, "last_extraction.json")
    assert paths.DB_PATH == os.path.join(root, "scheduler.db")  # same folder as the cache


class _ApiStatusError(Exception):
    """Duck-types an SDK API error (e.g. a Groq 500): just a status_code."""
    status_code = 500


@pytest.mark.parametrize("error", [ConnectionError("network is down"), _ApiStatusError("server error"),
                                   ImportError("No module named 'openai'")])
def test_any_backend_failure_is_reported_not_raised(env, error):
    conn, sid, img, cache = env
    def failing(data, media_type):
        raise error
    shown = []
    assert not run_import(conn, sid, str(img), ask=Recorder(YES), show=shown.append,
                          extractor=failing, cache_path=cache)
    assert any("Extraction failed" in s for s in shown)


def test_broken_pdf_is_reported_not_raised(env, tmp_path):
    conn, sid, _, cache = env
    bad = tmp_path / "broken.pdf"; bad.write_bytes(b"not really a pdf")
    shown = []
    assert not run_import(conn, sid, str(bad), ask=Recorder([]), show=shown.append, cache_path=cache)
    assert any("could not be opened as a PDF" in s for s in shown)


def test_unwritable_cache_still_reviews_and_saves(env, tmp_path):
    conn, sid, img, _ = env
    shown = []
    ok = run_import(conn, sid, str(img), ask=Recorder(YES + KEEP_ALL), show=shown.append,
                    extractor=lambda d, m: _extraction(), cache_path=str(tmp_path))  # a folder
    assert ok and _titles(conn, sid) == (["DS"], ["Lab"], ["HW"])
    assert any("couldn't save a replay copy" in s for s in shown)


@pytest.mark.parametrize("bug", [TypeError("bad arg"), AttributeError("no attr"), IndexError("empty")])
def test_a_bug_in_our_own_code_is_not_hidden(env, bug):
    conn, sid, img, cache = env
    def buggy(data, media_type):
        raise bug
    with pytest.raises(type(bug)):
        run_import(conn, sid, str(img), ask=Recorder(YES), show=lambda _: None,
                   extractor=buggy, cache_path=cache)
    assert _titles(conn, sid) == ([], [], [])


def test_json_replay_saved_with_a_bom_still_loads(env, tmp_path):
    conn, sid, _, cache = env
    saved = tmp_path / "bom.json"
    saved.write_bytes(b"\xef\xbb\xbf" + _extraction().model_dump_json().encode("utf-8"))
    assert run_import(conn, sid, str(saved), ask=Recorder(KEEP_ALL), show=lambda _: None, cache_path=cache)
    assert _titles(conn, sid) == (["DS"], ["Lab"], ["HW"])


# ---- a PDF where only some pages were read (option b: review it, save defaults to No) ----
def _partial_pdf_import(env, monkeypatch, tmp_path, answers):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    from scheduler.pdf_extraction import PartialExtraction
    import io
    conn, sid, _, cache = env
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.drawString(72, 700, "page one"); c.showPage()
    c.save()
    pdf_path = tmp_path / "partial.pdf"
    pdf_path.write_bytes(buf.getvalue())
    def partly(file_bytes, call_text_llm=None, call_vision=None):
        raise PartialExtraction(_extraction("Read"), [2, 3], RuntimeError("rate limit"))
    monkeypatch.setattr("scheduler.import_flow.extract_schedule_from_pdf", partly)
    ask, shown = Recorder(answers), []
    ok = run_import(conn, sid, str(pdf_path), ask=ask, show=shown.append, cache_path=cache)
    return ok, ask, shown

def test_partial_pdf_is_shown_but_enter_saves_nothing(env, monkeypatch, tmp_path):
    conn, sid, _, cache = env
    ok, ask, shown = _partial_pdf_import(env, monkeypatch, tmp_path, YES + KEEP_ALL + [""])
    assert not ok and _titles(conn, sid) == ([], [], [])
    assert any("only part of the PDF" in s for s in shown)
    assert "Page(s) 2, 3 weren't read" in ask.prompts[-1]
    assert ExtractionResult.model_validate_json(open(cache, encoding="utf-8").read()) == _extraction("Read")

def test_partial_pdf_can_still_be_saved_on_yes(env, monkeypatch, tmp_path):
    conn, sid, _, _ = env
    ok, _, _ = _partial_pdf_import(env, monkeypatch, tmp_path, YES + KEEP_ALL + ["y"])
    assert ok and _titles(conn, sid) == (["Read"], ["Lab"], ["HW"])


# ---- fix/import-partial: the replay copy remembers which pages weren't read ----

def _one_page_pdf(tmp_path):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    pdf_path = tmp_path / "p.pdf"
    c = canvas.Canvas(str(pdf_path), pagesize=letter)
    c.drawString(72, 700, "x"); c.showPage(); c.save()
    return str(pdf_path)

def _pdf_reads(monkeypatch, result, failed):
    from scheduler.pdf_extraction import PartialExtraction
    def partly(file_bytes, call_text_llm=None, call_vision=None):
        raise PartialExtraction(result, failed, RuntimeError("rate limit"))
    monkeypatch.setattr("scheduler.import_flow.extract_schedule_from_pdf", partly)

def _replay_with(tmp_path, failed_json):
    saved = tmp_path / "saved.json"
    saved.write_text(_extraction().model_dump_json()[:-1] + f', "failed_pages": {failed_json}}}', encoding="utf-8")
    return str(saved)

def test_replaying_a_partial_import_still_warns_it_is_incomplete(env, monkeypatch, tmp_path):
    conn, sid, _, cache = env
    _partial_pdf_import(env, monkeypatch, tmp_path, YES + KEEP_ALL + [""])  # declined: nothing saved
    ask = Recorder(KEEP_ALL + [""])
    ok = run_import(conn, sid, cache, ask=ask, show=lambda _: None, cache_path=str(tmp_path / "c2.json"))
    assert not ok and _titles(conn, sid) == ([], [], [])
    assert "Page(s) 2, 3 weren't read" in ask.prompts[-1]

def test_replay_copy_of_a_full_import_asks_nothing_extra(env, tmp_path):
    conn, sid, img, cache = env
    run_import(conn, sid, str(img), ask=Recorder(YES + KEEP_ALL), show=lambda _: None,
               extractor=lambda d, m: _extraction(), cache_path=cache)
    ask = Recorder(KEEP_ALL)
    assert run_import(conn, sid, cache, ask=ask, show=lambda _: None, cache_path=str(tmp_path / "c2.json"))
    assert len(ask.prompts) == 1  # only the review

@pytest.mark.parametrize("failed", ['"2"', '["two"]', "{}", "[true]", "[0]", "[-1]", "[1.5]"])
def test_replay_with_bad_failed_pages_is_reported(env, tmp_path, failed):
    conn, sid, _, cache = env
    shown = []
    assert not run_import(conn, sid, _replay_with(tmp_path, failed), ask=Recorder([]), show=shown.append,
                          cache_path=cache)
    assert any("not a valid saved extraction" in s for s in shown)

def test_replay_with_null_failed_pages_is_a_normal_replay(env, tmp_path):
    conn, sid, _, cache = env
    ask = Recorder(KEEP_ALL)
    assert run_import(conn, sid, _replay_with(tmp_path, "null"), ask=ask, show=lambda _: None, cache_path=cache)
    assert len(ask.prompts) == 1

def test_replay_failed_pages_are_sorted_and_unique(env, tmp_path):
    conn, sid, _, cache = env
    ask = Recorder(KEEP_ALL + [""])
    run_import(conn, sid, _replay_with(tmp_path, "[3, 2, 2]"), ask=ask, show=lambda _: None, cache_path=cache)
    assert "Page(s) 2, 3 weren't read" in ask.prompts[-1]

def test_declining_without_a_replay_copy_does_not_promise_one(env, monkeypatch, tmp_path):
    conn, sid, _, _ = env
    _pdf_reads(monkeypatch, _extraction("Read"), [2])
    shown = []
    assert not run_import(conn, sid, _one_page_pdf(tmp_path), ask=Recorder(YES + KEEP_ALL + [""]),
                          show=shown.append, cache_path=str(tmp_path / "no_such_dir" / "cache.json"))
    assert not any("kept in '" in s for s in shown)

def test_nothing_found_but_pages_failed_says_so(env, monkeypatch, tmp_path):
    conn, sid, _, cache = env
    _pdf_reads(monkeypatch, ExtractionResult(), [2, 3])
    shown = []
    assert not run_import(conn, sid, _one_page_pdf(tmp_path), ask=Recorder(YES), show=shown.append, cache_path=cache)
    assert any("page(s) 2, 3 could not be read" in s for s in shown)  # from the warning, shown once
    assert any("Nothing was found in the pages that were read" in s for s in shown)

def test_a_wrongly_shaped_image_answer_fails_and_keeps_the_old_replay_copy(env):
    from scheduler.llm_backends import BadModelOutput
    conn, sid, img, cache = env
    run_import(conn, sid, str(img), ask=Recorder(YES + KEEP_ALL), show=lambda _: None,
               extractor=lambda d, m: _extraction("Good"), cache_path=cache)
    def bad(data, media_type):
        raise BadModelOutput("The model sent 'tasks' in the wrong shape (int). Try again.")
    shown = []
    assert not run_import(conn, sid, str(img), ask=Recorder(YES), show=shown.append, extractor=bad, cache_path=cache)
    assert any("Extraction failed" in s and "Try again" in s for s in shown)
    assert ExtractionResult.model_validate_json(open(cache, encoding="utf-8").read()).weekly_patterns[0].title == "Good"


def test_declining_a_replay_says_the_replayed_file_still_holds_it(env, tmp_path):
    conn, sid, _, cache = env
    path = _replay_with(tmp_path, "[2]")
    shown = []
    run_import(conn, sid, path, ask=Recorder(KEEP_ALL + [""]), show=shown.append, cache_path=cache)
    assert any(f"kept in '{path}'" in s for s in shown)

def test_a_replay_gives_a_plain_extraction_result(env, tmp_path, monkeypatch):
    conn, sid, _, cache = env
    seen = []
    import scheduler.import_flow as flow
    real = flow.review_extraction
    monkeypatch.setattr(flow, "review_extraction", lambda result, **kw: seen.append(type(result)) or real(result, **kw))
    run_import(conn, sid, _replay_with(tmp_path, "[2]"), ask=Recorder(KEEP_ALL + [""]), show=lambda _: None,
               cache_path=cache)
    assert seen == [ExtractionResult]


def test_a_failed_replay_copy_write_keeps_the_old_copy(env, monkeypatch):
    conn, sid, img, cache = env
    run_import(conn, sid, str(img), ask=Recorder(YES + KEEP_ALL), show=lambda _: None,
               extractor=lambda d, m: _extraction("Old"), cache_path=cache)
    import scheduler.import_flow as flow
    def fail_replace(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(flow.os, "replace", fail_replace)
    run_import(conn, sid, str(img), ask=Recorder(YES + KEEP_ALL), show=lambda _: None,
               extractor=lambda d, m: _extraction("New"), cache_path=cache)
    assert ExtractionResult.model_validate_json(open(cache, encoding="utf-8").read()).weekly_patterns[0].title == "Old"


def test_a_failed_replay_copy_write_leaves_no_temp_file(env, monkeypatch):
    import os
    conn, sid, img, cache = env
    import scheduler.import_flow as flow
    def fail_replace(src, dst):
        raise OSError("locked by sync")
    monkeypatch.setattr(flow.os, "replace", fail_replace)
    run_import(conn, sid, str(img), ask=Recorder(YES + KEEP_ALL), show=lambda _: None,
               extractor=lambda d, m: _extraction(), cache_path=cache)
    assert not os.path.exists(cache + ".tmp")


def test_a_failed_replay_copy_write_warns_the_old_copy_is_from_earlier(env, monkeypatch):
    conn, sid, img, cache = env
    run_import(conn, sid, str(img), ask=Recorder(YES + KEEP_ALL), show=lambda _: None,
               extractor=lambda d, m: _extraction("Old"), cache_path=cache)
    import scheduler.import_flow as flow
    def fail_replace(src, dst):
        raise OSError("locked by sync")
    monkeypatch.setattr(flow.os, "replace", fail_replace)
    shown = []
    run_import(conn, sid, str(img), ask=Recorder(YES + KEEP_ALL), show=shown.append,
               extractor=lambda d, m: _extraction("New"), cache_path=cache)
    assert any("still holds an EARLIER import" in s for s in shown)


def test_the_replay_copy_path_can_be_a_path_object(env, tmp_path):
    conn, sid, img, _ = env
    cache = tmp_path / "c.json"
    assert run_import(conn, sid, str(img), ask=Recorder(YES + KEEP_ALL), show=lambda _: None,
                      extractor=lambda d, m: _extraction(), cache_path=cache)
    assert cache.exists()


def _past_and_future():
    return ExtractionResult(tasks=[ExtractedTask(title="Quiz 1", date="2026-09-20"),
                                   ExtractedTask(title="Quiz 2", date="2026-09-22", due_time="10:00"),
                                   ExtractedTask(title="Final", date="2026-12-10")])


def test_past_due_imported_tasks_are_saved_as_history_done_or_missed(env, tmp_path):
    from scheduler.planner import plan_from_saved
    conn, sid, _, cache = env
    saved = tmp_path / "syllabus.json"
    saved.write_text(_past_and_future().model_dump_json(), encoding="utf-8")
    shown = []
    ask = Recorder(KEEP_ALL + ["x", "", "m"])  # bad answer re-asked, Quiz 1 done, Quiz 2 missed
    now = datetime(2026, 10, 1, 9, 0)
    assert run_import(conn, sid, str(saved), ask=ask, show=shown.append, cache_path=cache, now=now)
    tasks = {t.title: t for _, t in get_extracted_tasks(conn, sid)}
    assert (tasks["Quiz 1"].completed_at, tasks["Quiz 1"].missed) == ("2026-10-01T09:00", False)
    assert tasks["Quiz 2"].completed_at and tasks["Quiz 2"].missed
    assert tasks["Final"].completed_at is None  # future tasks aren't asked about
    assert sum("was due" in p for p in ask.prompts) == 3 and "Type d or m." in shown
    assert any("2 past one(s) kept as history" in s for s in shown)
    _, _, _, warnings = plan_from_saved(conn, sid, now=now, time_limit_seconds=10)
    assert not [w for w in warnings if w.kind == "task_overdue"]


def test_a_saved_task_from_before_missed_existed_still_loads(env):
    conn, sid, _, _ = env
    conn.execute("INSERT INTO extracted_tasks (student_id, data_json, created_at) VALUES (?, ?, ?)",
                 (sid, '{"title": "Old", "date": "2026-10-02"}', "2026-09-01T00:00"))
    [(_, t)] = get_extracted_tasks(conn, sid)
    assert t.title == "Old" and t.missed is False
