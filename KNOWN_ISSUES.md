# Known issues

Zane will review and correct each. Referenced from `CLAUDE.md`.

1. `README.md` is stale ("V1 in progress").
2. `scheduler/sample_timetables/` contains real classmates' timetables in a public repo. Data hygiene is NOT done: needs synthetic replacements and a git-history scrub (back up and make the repo private first).
3. Model IDs live only in `llm_backends.py` (`DEFAULT_*`), overridable via `GROQ_MODEL` and `GROQ_VISION_MODEL`. Groq model names churn.
4. Known extraction misreads (rotated images, one of the sample PDFs) are possibly a vision-model quality issue, deferred to the model comparison. Not a pipeline bug.
5. No CI yet. Merges to master should go through a PR with green tests.
6. `PRD.md` is referenced in `CLAUDE.md` but is not in the repo.
7. Anthropic was removed entirely (code, tests, requirements). If it returns at 4.1, install it, pin its exact version in `requirements.txt`, and note that `claude-sonnet-5-5` rejects a forced `tool_choice`.
8. `DB_PATH` and `CACHE_PATH` are fixed to the repo root (`scheduler/paths.py`). A `scheduler.db` made by running scripts from another folder is not picked up; no migration (only Zane uses the app).
9. `requirements.txt` is a full pip freeze of the Windows/Python 3.14 `.venv`, including indirect packages. Before CI, split it into direct dependencies plus a lock file. Keep it UTF-8: in PowerShell 5.1, `pip freeze > requirements.txt` writes UTF-16 and breaks `pip install -r`. Use `pip freeze | Out-File -Encoding utf8 requirements.txt` or run it from Git Bash.
10. PDF text extraction often fails on `gpt-oss-120b` (`tool_use_failed`), so each page falls back to vision and costs two calls, which is the main cause of free-tier 429s. Address in model routing (4.0/4.1): retry once, trim the schema, or use another model; make the live PDF test catch the fallback.
11. Sleep target vs task fit: the solver can trade target sleep for a task, which breaks the "sleep target > task fit" order. The task bonus (`priority * presence_bonus`) is paid once per task, while target sleep costs `sleep_target_penalty` per slot, so even a 15-minute priority-5 task can take up to 2.5h. For now it is shown as a warning after "Added." Rescaling needs the drop menu's "sleep below target" choice stored per task, so that `plan_from_saved` honours it.
12. Hard-coded search tunables: `propose_drops` (`max_actions`, `max_proposals`, `max_checks`, `time_limit_seconds`) and the `restore.plan_restores` steps should be `ProfileSettings` fields with `POLICY` entries. Do this with the optimal-schedule work.
14. Unused reminder fields on `ExtractedTask` (`reminders_enabled`, `reminder_min_*`) are sent in the Groq extraction schema. Remove them or mark them `SkipJsonSchema` (see 10).
15. Commutes: adding one doesn't check for overlaps or ask the student (it breaks the commute invariant; planning only gives a soft warning).
16. Small CLI issues:
    - "inf" as hours crashes (`review.hours_to_slots` raises OverflowError).
    - `24:00` is rejected as an end time when editing an import (`review._time`).
    - Old reminder sessions with reminders turned off are never marked as asked (`completion.py`), so they pile up.
17. A due time inside the current 15-minute slot (e.g. due 10:15 at 10:05) passes `prompt_new_task`, but plans start at the next slot, so it can never be placed and the drop menu opens for nothing. Reject `due_slot <= next_slot(now)` on the same day.
20. `reflect.py` treats every `ValueError` (including our own `PreferenceError`) as a backend failure and offers another Groq call. Narrow it to real backend errors.
21. Duplicate helpers: `task_manager._ask_field` ~ `menu_input.ask_until`; `task_manager._rating` ~ `settings_menu._rating`; `commute_menu._clock` and `settings_menu._clock` re-parse HH:MM instead of using `units.parse_time` (see 15).

24. Preference storage in `db.py` (for G7): `load_tiers` writes (backfills default tiers) on every read; `load_evidence` and `load_evidence_times` read the same table in two queries; `get_reflections` returns loose dicts instead of a typed row (only tests read it).
