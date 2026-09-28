import builtins

import pytest

import reflect
from scheduler.db import connect, get_or_create_student, get_reflections


@pytest.fixture
def conn():
    return connect(":memory:")


@pytest.fixture(autouse=True)
def patch_connect(monkeypatch, conn):
    """reflect.main() calls connect(DB_PATH) internally -- redirect that to the
    fixture's in-memory connection so tests can inspect what got written, and so
    tests never touch a real scheduler.db file on disk.
    """
    monkeypatch.setattr(reflect, "connect", lambda path: conn)


def feed_inputs(monkeypatch, answers: list[str]):
    """Make input() return each of `answers` in order. Also prints the prompt text
    (real input() does this; a bare lambda replacement doesn't) so assertions can
    check prompts the same way you'd see them in a real run.
    """
    it = iter(answers)

    def fake_input(prompt=""):
        print(prompt, end="")
        return next(it)

    monkeypatch.setattr(builtins, "input", fake_input)


def test_full_run_with_accepted_proposal_updates_settings(monkeypatch, capsys):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    feed_inputs(monkeypatch, ["Zane", "felt rushed today, no breaks", "y"])

    reflect.main()

    out = capsys.readouterr().out
    assert "buffer_slots" in out
    assert "Applied 1/1 change(s)" in out


def test_full_run_with_rejected_proposal_leaves_settings_unchanged(monkeypatch, capsys):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    feed_inputs(monkeypatch, ["Zane", "felt rushed today, no breaks", "n"])

    reflect.main()

    out = capsys.readouterr().out
    assert "Applied 0/1 change(s)" in out


def test_run_with_no_matching_keywords_reports_no_changes(monkeypatch, capsys):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    feed_inputs(monkeypatch, ["Zane", "everything was fine, nothing to report"])

    reflect.main()

    out = capsys.readouterr().out
    assert "No changes proposed." in out


def test_non_fake_backend_asks_for_confirmation_first(monkeypatch, capsys):
    monkeypatch.setenv("LLM_BACKEND", "groq")
    feed_inputs(monkeypatch, ["n"])  # decline the cost warning

    reflect.main()

    out = capsys.readouterr().out
    assert "LLM_BACKEND=groq" in out
    assert "Aborted." in out


def test_declining_the_cost_warning_makes_no_db_changes(monkeypatch, capsys, conn):
    monkeypatch.setenv("LLM_BACKEND", "anthropic")
    feed_inputs(monkeypatch, ["n"])

    reflect.main()

    assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 0


def test_reflection_gets_logged_even_when_rejected(monkeypatch, conn):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    feed_inputs(monkeypatch, ["Zane", "felt rushed today, no breaks", "n"])

    reflect.main()

    sid = get_or_create_student(conn, "Zane")  # same connection reflect.main() used
    history = get_reflections(conn, sid)
    assert len(history) == 1
    assert history[0]["applied"] is False

