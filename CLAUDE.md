# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Auto Schedule Generator: students enter a fixed timetable, tasks (deadline, difficulty, priority) and sleep needs; the app builds a plan that fits everything while protecting sleep, with everything else being negotiable. The plan improves over time from the student's own daily/weekly reflections. V1 is the scheduler engine. It is a CLI-only Python app, with no web UI yet.

## How to work on this repo
- Ask before coding when ANYTHING is unclear. For non-trivial work, plan first and wait for approval.
- Walk Zane through each change in plain steps: what changed, why, how it connects. Line-by-line only when he asks.
- Small, reviewable diffs. No whole-file rewrites unless asked. He reviews with `/diff`.
- Run the tests yourself before presenting work. Add or update tests with every change.
- If a change affects an entry script (`main.py`, `manage_tasks.py`, etc.), update it and notify Zane.
- After each roadmap item, give a short flow test if necessary: which entry script to run, what to type, what he should see. He runs it and explains it back to another model which has repo access.
- One branch per task. Remind him when to commit and when to merge to master.
- Never touch or add real personal timetables or data. Use synthetic samples only.
- Never commit `.env` files or API keys (the repo is public).
- Keep deterministic logic (solver, fit check, drop ranking, evidence) out of the LLM's hands.
- Keep answers short.
- Update CLAUDE.md with progress after a section is complete and notify Zane before making the adjustments.

## Cost rule (important)

Never run anything that makes a real Anthropic call, without asking Zane first. Groq is valid since it is set up as an environment variable. Groq free tier is the only real backend in use right now. Anthropic is not to be used until the model comparison (PRD E2).

## Commands

The project uses Python 3.14 in `.venv` (Windows). Run everything from the repo root so `scheduler` imports resolve.

```powershell
.venv\Scripts\python.exe -m pytest                                  # all tests
.venv\Scripts\python.exe -m pytest tests/test_solver.py             # one file
.venv\Scripts\python.exe -m pytest tests/test_solver.py::test_name  # one test
.venv\Scripts\python.exe -m pytest -k "drop and not manual"         # by keyword
```

- Tests: `pytest -q` (must stay green).
- Demo (in-memory DB, not an app): `python main.py`
- There is no linter, formatter or build step configured.

## Conventions

- Interactive functions take `ask=input, show=print` (and often `now`/`today`) as injectable parameters. Tests drive them with scripted answer lists and fixed datetimes, so keep this pattern and never call `input()`, `print()` or `datetime.now()` deep inside the logic.

## LLM backends

All LLM access goes through `scheduler/llm_backends.py` (`call_llm` for text and `call_vision_llm` for images/PDFs). The `LLM_BACKEND` env var picks the backend:
- `fake` (default): offline, free, deterministic keyword heuristics. Tests rely on it, so never call a real LLM or make a test depend on a real backend.
- `groq`: needs `GROQ_API_KEY`. `GROQ_MODEL` and `GROQ_VISION_MODEL` override the models. It does not accept PDFs through vision.
- `anthropic`: needs `ANTHROPIC_API_KEY`. `ANTHROPIC_MODEL` overrides the model. This backend is paid.

Backend failures (missing key, retired model, rate limit, no tool call) raise a clear `RuntimeError`. Entry points report them using `llm_backends.is_backend_failure` instead of crashing; real code bugs still raise.

Every backend uses forced tool-calling with a JSON schema that comes from the pydantic models, so the output is structured, not free text.

## Invariants/Architecture: do not break these

**Time and data**
- 15-minute slots, 96/day, one continuous multi-day axis (`day * 96 + slot`). Day 0 = `PlanAnchor.start_date`: rolling, rebuilt every run, never stored. Sleep/bedtime may run past 96 (`end_slot` exclusive, max 192) to cross midnight. Use `time_to_slot` / `slot_to_time`.
- Two layers: stored calendar-based (`WeeklyPattern`, `DatedBlock`, `ExtractedTask`, `Commute`) vs solver-facing day-indexed (`FixedBlock`, `DynamicTask`, `SleepRule`). Convert only via `calendar_utils.build_plan_inputs` / `commutes.expand_commutes`.
- Every DB query is scoped by `student_id`. Schema changes are additive via `SCHEMA` + `_migrate`. Never drop or rewrite student data.

**Scheduling rules**
- Buffers: mandatory between task-task, task-block, block-task; not between two fixed blocks. After a commute: normal buffer. Before a commute: none.
- Sleep and deadlines are never silently traded away. Missing minimum sleep, skipping a night, or missing a deadline is a hard warning. Other drops are soft.
- Objective order (highest cost first): sleep minimum > sleep target > task fit/priority > same-day spread > bedtime drift.
- Commutes are fixed blocks with priority over tasks. If one overlaps another block, tell the student and ask; never silently drop it.
- Splitting a task is the student's choice (per-task `splittable`), never automatic.
- Plan cuts apply only to the current plan and never shrink the saved task.
- Imports overwrite only fixed blocks. Tasks are added individually, never replaced by an import.
- Completed tasks are kept as history, not deleted.

**Preferences and LLM**
- No hardcoded algorithm tunables: all live in `ProfileSettings`, per student.
- Every `ProfileSettings` field needs a `POLICY` entry (a test fails otherwise). Tiers: `LOCKED` (nobody), `USER` (student only), `MODEL_LEARNED` (reflections); students can claim a field.
- The LLM never picks values or thresholds. Reflections return direction + magnitude only. One reflection never changes a setting; it needs repeated evidence across separate reflections (`EVIDENCE_THRESHOLD`, `EVIDENCE_TTL_DAYS`).
- Penalty fields and `drop_deadline_multiplier` are never user-editable.
- Write settings via `preferences.set_values` / `user_edit` (they enforce tiers). Legacy path that skips tier checks via `db.save_settings`: `reflection_cycle.apply_and_log` only (the demo's y/N is the student's consent).

## Known issues (Zane will review and correct each)

1. `README.md` is stale ("V1 in progress").
4. `scheduler/sample_timetables/` contains real classmates' timetables in a public repo. Data hygiene is NOT done: needs synthetic replacements and a git-history scrub (back up and make the repo private first).
7. Model IDs live only in `llm_backends.py` (`DEFAULT_*`), overridable via `GROQ_MODEL`, `GROQ_VISION_MODEL`, `ANTHROPIC_MODEL`. `claude-sonnet-5` is a valid ID (checked Oct 2026). The newer `claude-sonnet-5-5` rejects the forced `tool_choice` the backends use, so E2 needs a code change before trying it. Groq model names churn.
9. Known extraction misreads (rotated images, `kayleigh_timetable.pdf`) are possibly a vision-model quality issue, deferred to the model comparison. Not a pipeline bug.
10. No CI yet. Merges to master should go through a PR with green tests.
11. `PRD.md` is referenced here but is not in the repo.
12. `anthropic` is commented out of `requirements.txt` until PRD E2, but `LLM_BACKEND=anthropic` is still selectable. Without the package, reflect and import report "No module named anthropic".
13. `DB_PATH` and `CACHE_PATH` are fixed to the repo root (`scheduler/paths.py`). A `scheduler.db` made by running scripts from another folder is not picked up; no migration (only Zane uses the app).
14. `requirements.txt` is a full pip freeze of the Windows/Python 3.14 `.venv`, including indirect packages. Before CI, split it into direct dependencies plus a lock file.

## Roadmap pointer (order only; details in PRD.md)

README + CI + data hygiene -> 3.7 -> 3.8 -> 4.0 provider-agnostic LLM client with usage logging, ruff/mypy -> 4.1 eval harness and model comparison (Haiku, Llama via Groq, Sonnet) -> 4.2 to 4.5 extraction and routing -> API layer -> cost gating -> frontend -> deployment -> v1.0. Then v1.1 re-planning agent, then learning from history and later features.

Future improvement: the solver and drop search find a *feasible* schedule (`max_checks` cuts the drop search short). A more efficient way to find the *optimal* schedule is planned.

Planned, not decided: Docker, Azure, LangGraph. Recommended, awaiting Zane's confirmation: FastAPI backend with a Streamlit v1.0 frontend (PRD section 12).
