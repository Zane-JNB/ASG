from datetime import datetime

from scheduler.calendar_utils import extracted_task_to_dynamic_task
from scheduler.db import get_extracted_tasks, get_plan_cuts
from scheduler.dropping import _solve_all_fit
from scheduler.fit_check import build_fit_inputs, starts_from


def plan_restores(conn, student_id: int, now: datetime, time_limit_seconds: float = 5.0,
                  steps: tuple[float, ...] = (1.0, 0.75, 0.5, 0.25)) -> dict[int, int]:   
    """{saved task id: slots that can be given back}. Cut tasks are tried highest priority first;
    each gets back as much of its cut as the solver can still fit with everything else, so
    every amount returned is verified before the student sees it."""
    cuts = get_plan_cuts(conn, student_id)
    if not cuts:
        return {}
    fit = build_fit_inputs(conn, student_id, now)
    saved = dict(get_extracted_tasks(conn, student_id))
    cap = fit.settings.default_max_session_slots
    planned = dict(fit.planned)  # id -> task with its cut applied (fully cut tasks are absent)
    active = {i: c for i, c in cuts.items()
              if i in saved and not saved[i].completed_at and saved[i].due_at() > now}  # overdue: nothing to give back
    order = sorted(active, key=lambda i: (-saved[i].priority, -saved[i].difficulty, i))
    given = {}
    for tid in order:
        for r in sorted({min(active[tid], max(1, round(active[tid] * f))) for f in steps}, reverse=True):
            trial = dict(planned)
            if tid in planned:
                trial[tid] = planned[tid].model_copy(update={"duration_slots": planned[tid].duration_slots + r})
            else:
                base = starts_from(extracted_task_to_dynamic_task(saved[tid], now.date(), cap), now)
                trial[tid] = base.model_copy(update={"duration_slots": r})
            if _solve_all_fit(fit.fixed, list(trial.values()), fit.anchor.num_days, fit.sleep_rules,
                              fit.settings, time_limit_seconds):
                planned, given[tid] = trial, r
                break
    return given