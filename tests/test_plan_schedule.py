import builtins

import plan_schedule
from scheduler.db import connect


def _run(monkeypatch, error):
    recorded = []
    def failing_plan(*a, **kw):
        raise error
    monkeypatch.setattr(plan_schedule, "connect", lambda path: connect(":memory:"))
    monkeypatch.setattr(builtins, "input", lambda prompt="": "Zane")
    monkeypatch.setattr(plan_schedule, "plan_from_saved", failing_plan)
    monkeypatch.setattr(plan_schedule, "record_plan", lambda *a, **kw: recorded.append(a))
    plan_schedule.main()
    return recorded


def test_solver_finding_nothing_is_reported_not_a_crash(monkeypatch, capsys):
    recorded = _run(monkeypatch, RuntimeError("No schedule found within 30.0s"))
    assert "No schedule found within 30.0s" in capsys.readouterr().out
    assert recorded == []


def test_bad_saved_data_is_still_reported(monkeypatch, capsys):
    recorded = _run(monkeypatch, ValueError("no saved schedule items -- run import_schedule.py first"))
    assert "run import_schedule.py first" in capsys.readouterr().out
    assert recorded == []
