import json
from types import SimpleNamespace

import pytest

from scheduler.schedule_extraction import extract_schedule


class FakeClient:
    """OpenAI-SDK-shaped stand-in (what Groq's client looks like): replies with one tool call."""
    def __init__(self, arguments: dict):
        self.chat = SimpleNamespace(completions=self)
        call = SimpleNamespace(function=SimpleNamespace(arguments=json.dumps(arguments)))
        self._response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call]))])

    def create(self, **kwargs):
        return self._response


def make_client(extraction: dict) -> FakeClient:
    return FakeClient(extraction)


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


def test_groq_vision_rejects_pdf_before_any_network_call(monkeypatch, real_groq_code):
    monkeypatch.setenv("GROQ_API_KEY", "unused-test-key")
    with pytest.raises(ValueError, match="doesn't support PDF"):
        extract_schedule(b"pdf bytes", "application/pdf")



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

# ---- the LLM layer owns its seams: a client stand-in, lenient list reading, one sort ----
from scheduler.llm_backends import BadModelOutput, as_items, call_llm, call_vision_llm
from scheduler.models import DatedBlock, ExtractedTask, ExtractionResult, WeeklyPattern
from scheduler.schedule_extraction import combine


def test_a_client_stand_in_runs_the_groq_code_even_while_the_tables_hold_stubs():
    """The provider tables hold the offline stubs (conftest); a client stand-in is OpenAI-SDK
    shaped, so it always goes through the real Groq request and returns what it sent."""
    sent = {"summary": "from the stand-in", "proposals": []}
    assert call_llm("system", "text", "tool", {}, client=FakeClient(sent)) == sent
    assert call_vision_llm("system", "text", "aGk=", "image/png", "tool", {}, client=FakeClient(sent)) == sent


@pytest.mark.parametrize("value,expected", [
    (None, []), ({}, []), ("N/A", []), ([{"title": "x"}], [{"title": "x"}]),
    ({"title": "x"}, [{"title": "x"}]), ('[{"title": "x"}]', [{"title": "x"}]),
])
def test_as_items_reads_what_models_send_for_a_list(value, expected):
    assert as_items("tasks", value) == expected


@pytest.mark.parametrize("value", [5, True, "a sentence", {"no_title": 1}])
def test_as_items_refuses_a_shape_it_cannot_trust(value):
    with pytest.raises(BadModelOutput):
        as_items("tasks", value)


def test_combine_merges_results_and_sorts_every_list():
    first = ExtractionResult(
        weekly_patterns=[WeeklyPattern(title="Fri", day="Fri", start_time="09:00", end_time="10:00")],
        tasks=[ExtractedTask(title="Late", date="2026-10-09")])
    second = ExtractionResult(
        weekly_patterns=[WeeklyPattern(title="Mon", day="Mon", start_time="11:00", end_time="12:00")],
        dated_blocks=[DatedBlock(title="B", date="2026-10-06", start_time="13:00", end_time="14:00"),
                      DatedBlock(title="A", date="2026-10-06", start_time="08:00", end_time="09:00")],
        tasks=[ExtractedTask(title="Early", date="2026-10-02", due_time="10:00")])
    merged = combine([first, second])
    assert [p.title for p in merged.weekly_patterns] == ["Mon", "Fri"]
    assert [b.title for b in merged.dated_blocks] == ["A", "B"]
    assert [t.title for t in merged.tasks] == ["Early", "Late"]
