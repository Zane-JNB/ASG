import pytest
from scheduler.db import connect, get_or_create_student

@pytest.fixture(autouse=True)
def offline_backend(monkeypatch):
    """Every test starts on the free offline backend with no real keys, even when the shell has
    LLM_BACKEND=groq and a real GROQ_API_KEY saved. Subprocesses (main.py) inherit this too.
    A test that wants another backend sets it itself, with a fake key."""
    monkeypatch.setenv("LLM_BACKEND", "fake")
    for key in ("GROQ_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)

@pytest.fixture
def conn(): return connect(":memory:")

@pytest.fixture
def sid(conn): return get_or_create_student(conn, "Zane")
