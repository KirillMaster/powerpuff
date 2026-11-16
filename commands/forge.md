---
description: forge — turnkey spec→program run. A script orchestrates; Opus audits the spec and does one fresh acceptance; Sonnet writes frozen acceptance tests and the code in one context. Resumable, zero questions
argument-hint: "<spec-dir | spec file | ticket text> [--preflight] [--writer sonnet|opus] [--max-usd N] [--max-hours N]"
---

# /forge — spec → turnkey program

The orchestrator is the script `${CLAUDE_PLUGIN_ROOT}/scripts/forge_run.py`, not you. Do not implement anything yourself and do not ask questions. Design: `docs/design-forge.md` in the plugin.

1. `<root>` = `git rev-parse --show-toplevel` (no repo → the current directory).
2. Start the driver detached from `<root>` and pass `$ARGUMENTS` through unchanged:
   - POSIX: `PYTHONUNBUFFERED=1 nohup python3 ${CLAUDE_PLUGIN_ROOT}/scripts/forge_run.py $ARGUMENTS > .forge-run.log 2>&1 &`
   - Windows: `powershell.exe -NoProfile -Command "Start-Process cmd -WindowStyle Hidden -ArgumentList '/c set PYTHONUNBUFFERED=1&& python ${CLAUDE_PLUGIN_ROOT}/scripts/forge_run.py $ARGUMENTS > .forge-run.log 2>&1'"`
   A ticket text with spaces: write it to `<root>/.forge-ticket.md` first and pass that path.
3. Tell the user in three lines: the run is detached; progress is in `<root>/.forge/progress.md`; the result is `<root>/.forge/REPORT.md` with the last log line `FORGE: done|failed`. Re-running the same command resumes from `.forge/state.json`.
4. `--preflight`: wait for the driver to print `FORGE: preflight`, then show the `Q` lines of `.forge/requirements.md` to the user as numbered questions; write the answers back as `D<n> (human): …` lines and rerun without `--preflight`.

What the driver does (every stage is one `claude -p` session, the script checks the output files):
| Stage | Model | Output |
|---|---|---|
| audit | `--model` (opus) | `.forge/requirements.md` (R/D/Q), `digest.md`, `gates.json` |
| tests | `--writer` (sonnet) | acceptance tests, frozen by sha256 |
| build | `--writer`, one context, ≤3 rounds, then one strong-model escalation | code; `forge_check.py` decides |
| accept | `--model`, fresh context | `.forge/gaps.json`; gaps → ≤2 fix rounds |
| report | script | `.forge/REPORT.md`, `FORGE: done|failed` |
