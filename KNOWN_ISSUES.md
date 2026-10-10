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
12. Hard-coded search tunables: `propose_drops` (`max_actions`, `max_proposals`, `max_checks`, `time_limit_seconds`) and the `restore.plan_restores` steps should be `ProfileSettings` fields with `POLICY` entries. Do this with the optimal-schedule work.


26. `propose_drops` re-solves "as things stand" before searching, although `add_task_with_fit` has just done that check (one extra solve per search). Keep it while `propose_drops` is also called directly (tests); drop it if the base check moves out of the engine.
