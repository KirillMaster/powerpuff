---
description: Autonomous run — start queue_run.py on queue.yaml in the background; a repeat resumes where it left off
argument-hint: "<queue.yaml> [--parallel N] [--no-wait] [--only U1,U2] [--dry-run] [--attended]"
---

# /powerpuff:queue — autonomous queue run

The orchestrator is the script `${CLAUDE_PLUGIN_ROOT}/scripts/queue_run.py`, not you. Do not implement anything and do not ask questions. Format: `docs/queue-format.md` in the plugin.

1. `<root>` = `git rev-parse --show-toplevel`.
2. `--dry-run`: run in the foreground and show the unit order. Otherwise start as a separate process from `<root>`, with `$ARGUMENTS` unchanged:
   - POSIX: `PYTHONUNBUFFERED=1 nohup python3 ${CLAUDE_PLUGIN_ROOT}/scripts/queue_run.py $ARGUMENTS --repo <root> > .queue-run.log 2>&1 &`
   - Windows: `powershell.exe -NoProfile -Command "Start-Process cmd -WindowStyle Hidden -ArgumentList '/c set PYTHONUNBUFFERED=1&& python ${CLAUDE_PLUGIN_ROOT}/scripts/queue_run.py $ARGUMENTS --repo <root> > .queue-run.log 2>&1'"`
3. Tell the human in three lines: the run was started separately; log — `<root>/.queue-run.log` (last line `QUEUE: done|partial|failed|config error`) and `<root>/.powerpuff/runs/<run_id>/<id>.log` per unit; state — `<root>/.powerpuff/runs/<run_id>/state.json`, summary — `SUMMARY.md` and `summary.json` next to it.
4. Repeating the same command continues the run: `needs-answer`, `blocked` and `skipped-budget` return to `pending`; answers to blocking questions are written to the unit's `.forge/questions.json` (the path is given in the reason). A config error (exit 3) prints all problems as a list, nothing is created.
5. Exit 3 with `--no-wait` = subscription limit; a repeat after the reset resumes. Without `--no-wait` the run waits for `resetsAt` itself and continues the same stage.
7. `--parallel N` (or `parallel:` in queue.yaml) — up to N independent units at once in one integration branch; slots shrink automatically as the 5-hour window fills up. A unit failure → one more attempt with the reason (`max_attempts`, 2), then `failed … needs a human`; `stuck` → `needs-answer`.
6. Units whose `risk` is not in `auto_merge` stay `done` until the audit.
