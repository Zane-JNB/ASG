import pytest

from scheduler.schedule_extraction import extract_schedule
from scheduler.llm_backends import call_vision_llm


class FakeBlock:
    def __init__(self, input_data):
        self.type = "tool_use"
        self.input = input_data


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeClient:
    def __init__(self, response):
        self.messages = self
        self._response = response

    def create(self, **kwargs):
        return self._response


def make_client(extraction: dict) -> FakeClient:
    return FakeClient(FakeResponse([FakeBlock(extraction)]))


def test_fake_backend_returns_empty_result_no_network(monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    result = extract_schedule(b"fake bytes", "image/png")
    assert result.weekly_patterns == []
    assert result.tasks == []


def test_client_override_extracts_weekly_pattern():
    client = make_client({
        "weekly_patterns": [
            {"title": "Class", "days_of_week": ["Mon", "Wed"], "start_time": "09:00", "end_time": "11:00"}
        ],
        "tasks": [],
    })
    result = extract_schedule(b"bytes", "image/png", client=client)
    assert len(result.weekly_patterns) == 1
    assert result.weekly_patterns[0].title == "Class"


def test_client_override_extracts_weekly_pattern():
    client = make_client({
        "weekly_patterns": [
            {"title": "Class", "day": "Mon", "start_time": "09:00", "end_time": "11:00"}
        ],
        "tasks": [],
    })
    result = extract_schedule(b"bytes", "image/png", client=client)
    assert len(result.weekly_patterns) == 1
    assert result.weekly_patterns[0].title == "Class"

def test_invalid_weekly_pattern_dropped_but_others_kept():
    client = make_client({
        "weekly_patterns": [
            {"title": "Good", "day": "Mon", "start_time": "09:00", "end_time": "11:00"},
            {"title": "Bad", "day": "Tue", "start_time": "11:00", "end_time": "09:00"},  # invalid
        ],
        "tasks": [],
    })
    result = extract_schedule(b"bytes", "image/png", client=client)
    assert [p.title for p in result.weekly_patterns] == ["Good"]


def test_empty_extraction_result():
    client = make_client({"weekly_patterns": [], "tasks": []})
    result = extract_schedule(b"bytes", "image/png", client=client)
    assert result.weekly_patterns == []
    assert result.tasks == []


def test_groq_vision_rejects_pdf_before_any_network_call(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "unused-fake-key")
    with pytest.raises(ValueError, match="doesn't support PDF"):
        extract_schedule(b"pdf bytes", "application/pdf")


def test_call_vision_llm_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "not-a-real-backend")
    with pytest.raises(ValueError, match="unknown LLM_BACKEND"):
        call_vision_llm("system", "text", "base64data", "image/png", "tool", {})


def test_call_vision_llm_fake_backend_needs_no_key(monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    result = call_vision_llm("system", "text", "base64data", "image/png", "tool", {})
    assert result == {"weekly_patterns": [], "tasks": []}

def test_weekly_patterns_sorted_by_weekday_regardless_of_input_order():
    client = make_client({
        "weekly_patterns": [
            {"title": "Friday class", "day": "Fri", "start_time": "09:00", "end_time": "10:00"},
            {"title": "Monday class", "day": "Mon", "start_time": "09:00", "end_time": "10:00"},
            {"title": "Wednesday class", "day": "Wed", "start_time": "09:00", "end_time": "10:00"},
        ],
        "tasks": [],
    })
    result = extract_schedule(b"bytes", "image/png", client=client)
    assert [p.day for p in result.weekly_patterns] == ["Mon", "Wed", "Fri"]


def test_weekly_patterns_sorted_by_time_within_same_day():
    client = make_client({
        "weekly_patterns": [
            {"title": "Afternoon", "day": "Mon", "start_time": "14:00", "end_time": "15:00"},
            {"title": "Morning", "day": "Mon", "start_time": "09:00", "end_time": "10:00"},
        ],
        "tasks": [],
    })
    result = extract_schedule(b"bytes", "image/png", client=client)
    assert [p.title for p in result.weekly_patterns] == ["Morning", "Afternoon"]


def test_dated_blocks_sorted_by_date_then_time():
    client = make_client({
        "weekly_patterns": [],
        "dated_blocks": [
            {"title": "Later date", "date": "2026-09-05", "start_time": "09:00", "end_time": "10:00"},
            {"title": "Earlier date, later time", "date": "2026-09-02", "start_time": "14:00", "end_time": "15:00"},
            {"title": "Earlier date, earlier time", "date": "2026-09-02", "start_time": "08:00", "end_time": "09:00"},
        ],
        "tasks": [],
    })
    result = extract_schedule(b"bytes", "image/jpeg", client=client)
    assert [b.title for b in result.dated_blocks] == [
        "Earlier date, earlier time", "Earlier date, later time", "Later date",
    ]


def test_tasks_sorted_by_date():
    client = make_client({
        "weekly_patterns": [],
        "tasks": [
            {"title": "Due later", "date": "2026-10-15"},
            {"title": "Due sooner", "date": "2026-09-20"},
        ],
    })
    result = extract_schedule(b"bytes", "image/png", client=client)
    assert [t.title for t in result.tasks] == ["Due sooner", "Due later"]