import builtins
import pytest
import reflect
from scheduler.reflection_cycle import reflect_and_record
from scheduler.db import connect, get_or_create_student, get_reflections
from scheduler.db import load_evidence, load_settings
from scheduler.preferences import Actor, change_tier, set_approval_mode, APPROVAL_ASK, Actor, OUTCOME_APPLIED, OUTCOME_AT_LIMIT, OUTCOME_EVIDENCE, OUTCOME_IGNORED, OUTCOME_NONE, OUTCOME_PENDING
from scheduler.preference_policy import Tier

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


def test_first_reflection_records_evidence_only(monkeypatch, capsys, conn): 
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    feed_inputs(monkeypatch, ["Zane", "felt rushed today, no breaks"])
    reflect.main()
    out = capsys.readouterr().out
    assert "Break time" in out and "1/3" in out and "Evidence recorded" in out
    sid = get_or_create_student(conn, "Zane")
    assert load_settings(conn, sid).buffer_slots == 1


def test_third_reflection_applies_one_learned_update(monkeypatch, capsys, conn):  
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    for _ in range(3):
        feed_inputs(monkeypatch, ["Zane", "felt rushed today, no breaks"])
        reflect.main()
    assert "Settings updated." in capsys.readouterr().out
    sid = get_or_create_student(conn, "Zane")
    assert load_settings(conn, sid).buffer_slots == 3 and load_evidence(conn, sid) == {}

def test_user_owned_field_is_reported_and_untouched(monkeypatch, capsys, conn):   
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    sid = get_or_create_student(conn, "Zane")
    change_tier(conn, sid, "buffer_slots", Tier.USER, Actor.USER)
    feed_inputs(monkeypatch, ["Zane", "felt rushed today, no breaks"])
    reflect.main()
    assert "set by you" in capsys.readouterr().out
    assert load_evidence(conn, sid) == {} and load_settings(conn, sid).buffer_slots == 1


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


def test_reflection_is_logged_with_outcome(monkeypatch, conn):   
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    feed_inputs(monkeypatch, ["Zane", "felt rushed today, no breaks"])
    reflect.main()
    h = get_reflections(conn, get_or_create_student(conn, "Zane"))
    assert len(h) == 1 and h[0]["applied"] is False and h[0]["outcome"] == "evidence_recorded"

def _reflect(monkeypatch, extra=()):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    feed_inputs(monkeypatch, ["Zane", "felt rushed today, no breaks", *extra])
    reflect.main()

def test_ask_mode_prompts_only_at_threshold_and_yes_applies(monkeypatch, capsys, conn):
    sid = get_or_create_student(conn, "Zane")
    set_approval_mode(conn, sid, APPROVAL_ASK, Actor.USER)
    _reflect(monkeypatch); _reflect(monkeypatch)                 # no 3rd input: no prompt yet
    assert load_settings(conn, sid).buffer_slots == 1
    _reflect(monkeypatch, ["y"])
    out = capsys.readouterr().out
    assert "Apply this change?" in out and "needs your approval" in out
    assert load_settings(conn, sid).buffer_slots == 3

def test_ask_mode_no_leaves_value_unchanged(monkeypatch, conn):
    sid = get_or_create_student(conn, "Zane")
    set_approval_mode(conn, sid, APPROVAL_ASK, Actor.USER)
    _reflect(monkeypatch); _reflect(monkeypatch); _reflect(monkeypatch, ["n"])
    assert load_settings(conn, sid).buffer_slots == 1 and load_evidence(conn, sid) == {}

def test_every_reflection_outcome_has_a_status_line():   
    assert {OUTCOME_APPLIED, OUTCOME_PENDING, OUTCOME_EVIDENCE,
            OUTCOME_AT_LIMIT, OUTCOME_IGNORED, OUTCOME_NONE} <= set(reflect._STATUS)
