import os

import pytest
from scheduler import llm_backends
from scheduler.db import connect, get_or_create_student

# Saved before any test deletes it, so @pytest.mark.live tests can put it back.
_REAL_GROQ_KEY = os.environ.get("GROQ_API_KEY")


def pytest_configure(config):
    config.addinivalue_line("markers", "live: makes a real Groq call (skipped without GROQ_API_KEY)")


def pytest_collection_modifyitems(config, items):
    """Live tests run only when asked for (`pytest -m live`), never in a plain `pytest` run."""
    if "live" in (config.option.markexpr or ""):
        return
    skip = pytest.mark.skip(reason="live Groq test: run with -m live")
    for item in items:
        if item.get_closest_marker("live"):
            item.add_marker(skip)


def stub_text_call(system_prompt, user_message, tool_name, tool_schema, client=None):
    """Test-only stand-in for Groq: keyword heuristics, deterministic, no network."""
    text = user_message.lower()
    proposals = []
    if any(w in text for w in ("rushed", "no break", "no time", "back to back")):
        proposals.append({"field": "buffer_slots", "direction": "increase", "magnitude": "medium",
                          "reason": "mentioned feeling rushed or lacking breaks (test stub)"})
    if any(w in text for w in ("tired", "exhausted", "didn't sleep", "not enough sleep")):
        proposals.append({"field": "sleep_target_penalty", "direction": "increase", "magnitude": "small",
                          "reason": "mentioned tiredness or insufficient sleep (test stub)"})
    return {"summary": "Test stub response -- no real LLM call was made.", "proposals": proposals}


def stub_vision_call(system_prompt, user_text, image_base64, media_type, tool_name, tool_schema,
                     client=None):
    """Test-only stand-in for Groq vision: can't read the image, returns an empty result."""
    return {"weekly_patterns": [], "tasks": []}


@pytest.fixture(autouse=True)
def offline_backend(request, monkeypatch):
    """Every test runs offline: no GROQ_API_KEY, and the provider tables point at the stubs above,
    even when the shell has a real key saved. Subprocesses (main.py) inherit the missing key.
    @pytest.mark.live tests get the real key and the real Groq calls back instead."""
    if request.node.get_closest_marker("live"):
        if not _REAL_GROQ_KEY:
            pytest.skip("live test: GROQ_API_KEY not set")
        monkeypatch.setenv("GROQ_API_KEY", _REAL_GROQ_KEY)
        return
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setitem(llm_backends._BACKENDS, "groq", stub_text_call)
    monkeypatch.setitem(llm_backends._VISION_BACKENDS, "groq", stub_vision_call)


@pytest.fixture
def real_groq_code(monkeypatch):
    """Put the real Groq functions back in the provider tables (still no key, no network):
    for tests that drive _groq_call through a stand-in OpenAI client."""
    monkeypatch.setitem(llm_backends._BACKENDS, "groq", llm_backends._groq_call)
    monkeypatch.setitem(llm_backends._VISION_BACKENDS, "groq", llm_backends._groq_vision_call)


@pytest.fixture
def conn(): return connect(":memory:")

@pytest.fixture
def sid(conn): return get_or_create_student(conn, "Zane")
