---
description: forge — turnkey spec→program run. A script orchestrates; Opus audits the spec and does one fresh acceptance; Sonnet writes frozen acceptance tests and the code in one context. Resumable; no questions during the run, a blocking question stops it
argument-hint: "<spec-dir | spec file | ticket text> [--preflight] [--assume-defaults] [--allow-red-base] [--final] [--writer sonnet|opus] [--max-usd N] [--max-hours N]"
---

# /forge — spec → turnkey program

The orchestrator is the script `${CLAUDE_PLUGIN_ROOT}/scripts/forge_run.py`, not you. Do not implement anything yourself and do not ask questions. Design: `docs/design-forge.md` in the plugin.

1. `<root>` = `git rev-parse --show-toplevel` (no repo → the current directory).
2. Start the driver detached from `<root>` and pass `$ARGUMENTS` through unchanged:
   - POSIX: `PYTHONUNBUFFERED=1 nohup python3 ${CLAUDE_PLUGIN_ROOT}/scripts/forge_run.py $ARGUMENTS > .forge-run.log 2>&1 &`
   - Windows: `powershell.exe -NoProfile -Command "Start-Process cmd -WindowStyle Hidden -ArgumentList '/c set PYTHONUNBUFFERED=1&& python ${CLAUDE_PLUGIN_ROOT}/scripts/forge_run.py $ARGUMENTS > .forge-run.log 2>&1'"`
   A ticket text with spaces: write it to `<root>/.forge-ticket.md` first and pass that path.
3. Tell the user in three lines: the run is detached; progress is in `<root>/.forge/progress.md`; the result is `<root>/.forge/REPORT.md` with the last log line `FORGE: done|failed`. Re-running the same command resumes from `.forge/state.json`.
4. `--preflight`: wait for the driver to print `FORGE: preflight`, then show the questions of `.forge/questions.json` (the driver prints the count per class) to the user; write each answer into its `"answer"` field and rerun without `--preflight`. Without `--assume-defaults` a run with an unanswered `blocking` question fails (`blocking questions: Q1,Q4`, exit 1) and continues from the same state once answered; answered questions reach the test-writer and builder as `Human decisions:`.
5. `--allow-red-base`: continue although gates are red on the untouched base tree (default: `FORGE: failed`, `base is red: <gates>`, nothing spent); a missing tool always fails.
6. `--final`: after green acceptance run the profile tier `final` once (red → `FORGE: failed`, `final gates red: <names>`, rerun repeats only this step). Without it tier `final` belongs to the end of a queue run (`queue_run.py`).
7. Gate commands may use `{base}` (the commit the run started from) `{root}` and `{plugin}` (plugin root); also exported as `POWERPUFF_BASE` / `POWERPUFF_ROOT` / `POWERPUFF_PLUGIN`. The run also writes `.forge/quality-report.json`.

What the driver does (every stage is one `claude -p` session, the script checks the output files):
| Stage | Model | Output |
|---|---|---|
| base check | script | all gates once on the untouched base (before the audit with a profile, after it otherwise); red → failed unless `--allow-red-base` |
| audit | `--model` (opus) | `.forge/requirements.md` (R/D/Q), `.forge/questions.json` (`Q<n> [blocking\|non-blocking\|info]: … \| default: … \| cost of a wrong guess: …`), `digest.md`; `gates.json` only without `powerpuff.yaml` (then the driver also writes `.forge/powerpuff.draft.yaml`) |
| tests | `--writer` (sonnet) | acceptance tests; the session may only add files: other edits are reverted, stray non-test files removed (manifests and profile `test_infra` globs excepted; REPORT line `test-writer changes reverted`); frozen by sha256; then the test gates (profile `test_gates`, else ids containing `test`) must fail without an implementation, else one `vacuous` session; REPORT line `acceptance tests fail before implementation` |
| build | `--writer`, one context, ≤3 rounds, then one strong-model escalation | code; `forge_check.py` + `gate_guard.py` decide (gates from `powerpuff.yaml`: tiers `iteration` + `unit`) |
| accept | `--model`, fresh context | `.forge/gaps.json`; gaps → ≤2 fix rounds |
| report | script | `.forge/REPORT.md` (line `gates:` says whether the gates are approved; line `guard:`), `FORGE: done|failed` |

Flags and exit codes of the driver:
| Flag / exit | Meaning |
|---|---|
| `--no-wait` | rate limit → emit the result and exit 3 instead of sleeping; API unreachable → exit 3 at once |
| `--max-outage-min N` (120) | API unreachable (network, 5xx): wait 30 s, 60 s, … ≤15 min per step, a try is not spent and the stage does not advance; after N minutes in total (or when `--max-hours` runs out) `FORGE: paused (API unreachable)`, exit 3, state unchanged, REPORT.md says it is NOT a verdict about the code; rerun continues. With `--stream` the result event carries `forge_outcome: "paused-api"` |
| exit 0 / 1 / 3 | `FORGE: done` / `FORGE: failed` / rate limit or API pause, resumable |

Gates: a missing tool fails the gate; edits of `.forge/gates.json` / `powerpuff.yaml` during a run are reverted and fail the check. A draft gate set stays "NOT APPROVED" until the user saves `.forge/powerpuff.draft.yaml` as `powerpuff.yaml` (set `approved: true`).
