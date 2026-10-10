# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Auto Schedule Generator: students enter a fixed timetable, tasks (deadline, difficulty, priority) and sleep needs; the app builds a plan that fits everything while protecting sleep, with everything else being negotiable. The plan improves over time from the student's own daily/weekly reflections. V1 is the scheduler engine. It is a CLI-only Python app, with no web UI yet.

## How to work on this repo
- Ask before coding when ANYTHING is unclear. For non-trivial work, plan first and wait for approval.
- Walk Zane through each change in plain steps: what changed, why, how it connects. Line-by-line only when he asks.
- Small, reviewable diffs. No whole-file rewrites unless asked.
- Run the tests yourself before presenting work. Add or update tests with every change.
- If a change affects an entry script (`main.py`, `manage_tasks.py`, etc.), update it and notify Zane.
- After each roadmap item, give a short flow test if necessary: which entry script to run, what to type, what he should see.
- One branch per task. Remind him when to commit and when to merge to master.
- Never touch or add real personal timetables or data. Use synthetic samples only.
- Never commit `.env` files or API keys (the repo is public).
- Keep deterministic logic (solver, fit check, drop ranking, evidence) out of the LLM's hands.
- Keep answers short.
- Always create excellent test cases for your mission or goal then create the functionality to solve those tests to prevent determinism
- Update CLAUDE.md with progress after a section is complete and notify Zane before making the adjustments.
- Use the "Explore -> Plan -> Code -> Commit" framework

## Cost rule (important)

Groq (free tier) is the only provider and may be called; it is set up as an environment variable. Never add or run a paid provider (e.g. Anthropic) without asking Zane first. Other providers come back only at the model comparison (4.1), see "LLM backends".

## Commands

The project uses Python 3.14 in `.venv` (Windows). Run everything from the repo root so `scheduler` imports resolve.

```powershell
.venv\Scripts\python.exe -m pytest                                  # all tests
.venv\Scripts\python.exe -m pytest tests/test_solver.py             # one file
.venv\Scripts\python.exe -m pytest tests/test_solver.py::test_name  # one test
.venv\Scripts\python.exe -m pytest -k "drop and not manual"         # by keyword
```

- Tests: `pytest -q` (must stay green). Runs fully offline: `tests/conftest.py` removes `GROQ_API_KEY` and swaps test-only stubs into the provider tables.
- Live tests: `pytest -m live` makes 3 real Groq calls (skipped without `GROQ_API_KEY`). Shape checks only.
- Demo (in-memory DB, not an app): `python main.py`
- There is no linter, formatter or build step configured.

## Conventions

- Interactive functions take `ask=input, show=print` (and often `now`/`today`) as injectable parameters. Tests drive them with scripted answer lists and fixed datetimes, so keep this pattern and never call `input()`, `print()` or `datetime.now()` deep inside the logic.

## LLM backends

All LLM access goes through `scheduler/llm_backends.py` (`call_llm` for text and `call_vision_llm` for images). Groq is the only provider: `PROVIDER = "groq"` picks from the `_BACKENDS` / `_VISION_BACKENDS` tables. There is no `LLM_BACKEND` env var and no offline/fake backend in the app.
- Groq needs `GROQ_API_KEY`. `GROQ_MODEL` and `GROQ_VISION_MODEL` override the models. Its vision model takes images only; PDFs go through `pdf_extraction` (text per page, scanned pages rendered to images).
- Entry points (`reflect.py`, import) always ask before the Groq call. `main.py`'s demo makes one Groq call and skips the reflection part if it fails.
- Tests never call Groq except `@pytest.mark.live` ones. Code that takes a `client=` stand-in expects an OpenAI-SDK shape (`chat.completions.create`).

Plan for more providers (model comparison, 4.1): each candidate model gets its own branch; suitable ones are added to the provider tables and chosen by a task/depth-based router (4.0) that replaces `PROVIDER`. Routing goes by task (reflection / image / PDF) and depth (page count, text length, image size), optionally with a fallback model on a 429.
Backend failures (missing key, retired model, rate limit, no tool call) raise a clear `RuntimeError`. Entry points report them using `llm_backends.is_backend_failure` instead of crashing; real code bugs still raise.

Every backend uses forced tool-calling with a JSON schema that comes from the pydantic models, so the output is structured, not free text.

## Invariants/Architecture: do not break these

**Time and data**
- 15-minute slots, 96/day, one continuous multi-day axis (`day * 96 + slot`). Day 0 = `PlanAnchor.start_date`: rolling, rebuilt every run, never stored. Sleep/bedtime may run past 96 (`end_slot` exclusive, max 192) to cross midnight. Every slot/time/date/weekday/hours helper lives in `scheduler/units.py` (`time_to_slot`, `slot_to_time`, `parse_time`, `parse_date`, `format_hours`, `next_slot`, ...); don't re-parse HH:MM elsewhere.
- Two layers: stored calendar-based (`WeeklyPattern`, `DatedBlock`, `ExtractedTask`, `Commute`) vs solver-facing day-indexed (`FixedBlock`, `DynamicTask`, `SleepRule`). Convert only via `calendar_utils.expand_fixed_blocks` / `calendar_utils.extracted_task_to_dynamic_task` / `commutes.expand_commutes`.
- Every DB query is scoped by `student_id`. Schema changes are additive via `SCHEMA` + `_migrate`. Never drop or rewrite student data.
- All SQL lives in `db.py`. One commit rule: every write runs inside `db.transaction(conn)` (nested blocks join the outer one; only the outermost commits, any error rolls the whole block back). Group related writes in one `with transaction(conn):`; never call `conn.commit()`.
- Saved planner rows go through the `db.ItemTable` stores (`WEEKLY_PATTERNS`, `DATED_BLOCKS`, `EXTRACTED_TASKS`, `COMMUTES`; `PLANNER_TABLES` = all four): `add`/`read`/`get`/`find`/`update`/`delete`/`clear`.
- `plan_from_saved` always plans from now, through `build_fit_inputs` (the fit check's window). There is no fixed start-date mode.
- Saved rows that fail their model checks are skipped, never dropped or rewritten, and get a hard `saved_row_unreadable` warning (`db.get_unreadable_items`); the student deletes them via `[u]` in `manage_tasks.py`.

**Scheduling rules**
- Buffers: mandatory between task-task, task-block, block-task; not between two fixed blocks. After a commute: normal buffer. Before a commute: none. After waking: `wake_buffer_slots` (default 1h, student-editable) before any task, class or commute, every night, instead of the normal buffer (solver: each night's interval covers sleep + wake buffer; none on a night with no sleep). This morning's version is the "Sleep (night before)" + "Getting ready" blocks (`nights.sleep_setup`).
- Sleep and deadlines are never silently traded away. Missing minimum sleep, skipping a night, or missing a deadline is a hard warning. Other drops are soft. Open tasks past their due date and optional `due_time` (rounded down to its slot, like the solver deadline) get a hard `task_overdue` warning (`fit_check._planned_and_overdue`). A task whose plan cut takes all its time gets a hard `task_dropped` warning on every plan; a partly cut one gets a soft `task_cut`.
- Overlapping saved classes/sessions are a hard `block_overlap` warning, never an error: both are kept and the solver plans around their union. Import asks before saving blocks that clash with the saved table it doesn't replace.
- Every night must end by `SleepRule.latest_wake`: the next day's first class/commute minus `wake_buffer_slots`, with `latest_wake_reason` naming it (`nights.with_wake_limit`). For the window's last night that is the morning after the window, which is only looked at, never planned, so the window doesn't grow. Drop proposals count only sleep below what each night allows (`solver.reachable_sleep`), and a short-sleep warning names the cap only when it was the limit.
- Objective order (highest cost first): sleep minimum > sleep target > task fit/priority > same-day spread > bedtime drift.
- Commutes are fixed blocks with priority over tasks. If one overlaps another block, tell the student and ask; never silently drop it.
- Splitting a task is the student's choice (per-task `splittable`), never automatic.
- Plan cuts apply only to the current plan and never shrink the saved task.
- Imports overwrite only fixed blocks. Tasks are added individually, never replaced by an import.
- Completed tasks are kept as history, not deleted. A task closed without being done is kept too, with `missed=True` (`[f]` in `manage_tasks.py` asks done or missed). Imported tasks already past due are asked done/missed and saved closed (`import_flow._close_past_tasks`).

**Preferences and LLM**
- No hardcoded algorithm tunables: all live in `ProfileSettings`, per student.
- Every `ProfileSettings` field needs a `POLICY` entry (a test fails otherwise). Tiers: `LOCKED` (nobody), `USER` (student only), `MODEL_LEARNED` (reflections); students can claim a field.
- The LLM never picks values or thresholds. Reflections return direction + magnitude only. One reflection never changes a setting; it needs repeated evidence across separate reflections (`EVIDENCE_THRESHOLD`, `EVIDENCE_TTL_DAYS`).
- Penalty fields and `drop_deadline_multiplier` are never user-editable.
- Write settings via `preferences.set_values` / `user_edit` (they enforce tiers). Legacy path that skips tier checks via `db.save_settings`: `reflection_cycle.apply_and_log` only (the demo's y/N is the student's consent).

## Known issues (Zane will review and correct each)

See @KNOWN_ISSUES.md for the numbered list. Add new issues there, not here.

## Roadmap pointer (order only; details in PRD.md)

README + CI + data hygiene -> 3.7 -> 3.8 -> 4.0 provider-agnostic LLM client with usage logging, ruff/mypy -> 4.1 eval harness and model comparison (Haiku, Llama via Groq, Sonnet) -> 4.2 to 4.5 extraction and routing -> API layer -> cost gating -> frontend -> deployment -> v1.0. Then v1.1 re-planning agent, then learning from history and later features.

Future improvement: the solver and drop search find a *feasible* schedule (`max_checks` cuts the drop search short). A more efficient way to find the *optimal* schedule is planned.

Planned, not decided: Docker, Azure, LangGraph. Recommended, awaiting Zane's confirmation: FastAPI backend with a Streamlit v1.0 frontend (PRD section 12).
