# Handoff: powerpuff refactor (cost + speed), 2026-11-22

## Goal of the next session
1. Refactor the powerpuff plugin (`C:/pet/powerpuff`, GitHub KirillMaster/powerpuff, v0.2.0) per the list below.
2. Update integrations: `C:/pet/vetal_project` (ppar), `C:/pet/dasha_b_rabota_bot`, `C:/pet/tictactoe` (all have `powerpuff.yaml`).
3. Restart vetal_project from where it stopped.

## Data (night 21→22.11, vetal_project)
- Full report: `C:/Work/notes/claude-usage-vetal-powerpuff-2026-11-22.md`. Langfuse: http://localhost:3000/project/claude-code (up).
- 12 h: 3,640 Opus calls, 618M cache-read tokens, ≈$544 at API prices (~34% of the weekly limit). 81 runs, 64 units in `queue.yaml`.
- Status ~11:08: 39 merged, 4 running (U33, U39, U42, U53), 1 failed (U01), 20 not started (U10b U13b U36 U40 U41 U43–U47 U49 U51 U52 U55–U60 U62).
- U07 waited for an answer to Q1 (`.forge/questions.json`), then was merged.
- Time (28.6 unit-hours): plain-build 27%, **rate-limit pause 21% (6 h, 98 pauses)**, plain-tests 15%, setup (`dotnet restore`) 9%, check gates 6%. Idle windows in ppar-main.log: 02:48→04:52 (124 min), 09:12→10:32 (79 min) and 6 more of ~20–30 min.
- Money: plain-tests 44%, plain-build 40%, cover 10%, fix 6%; did not reach merge 41% ($176).

## Problem → solution
| # | Problem | Solution | Where | Effect |
|---|---|---|---|---|
| 1 | The whole spec is baked into every unit's prompt, ~211K characters (~100K tokens): spec.yaml 37K, tasks.yaml 60K, spec.md, tasks.md, contracts, data-model; the unit's own spec is 2K. Every call rereads ~170K | Put only the unit spec + its T-tasks + slices of contracts/data-model by the unit's `requirements`/`files` in the prompt; give the rest as paths for Read. Target ≤15K | `forge_run.py` (plain-tests prompt assembly), `queue_run.py` (unit spec) | −40–45% $ and each call is faster |
| 2 | Rate limit `five_hour rejected` → `FORGE: paused` → the unit goes back to pending → ppar starts it again from scratch (25 abandoned runs, $42) | Pause = wait for `resetsAt` from `rate_limit_event` and continue the same session/stage; do not create a new run | `forge_run.py:391 session()` (RateLimited), `ppar.py` (pending handling) | −10%, no lost work |
| 3 | Unbounded retries: U05 ×6, U07 ×5, U04 ×4; $68 went to retries of units that later merged | At most 2 runs per unit, then `needs-human`; on retry pass `.forge/disputes.md` and the reason for the previous failure | `ppar.py` / `queue_run.py` | −10–15% |
| 4 | A fix round changes nothing in the tree (19 times), yet the unit is still restarted | "no change to the tree" → straight to `needs-answer` with the question from disputes, no retry | `forge_run.py` (fix loop) | fewer empty runs |
| 5 | Everything on Opus, including build/fix (46% $) | Separate `model_build` / `model_fix` parameters (Sonnet), keep Opus for plain-tests | `queue_run.py:426`, `forge_run.py --model` | −15–18% (watch the number of fix rounds) |
| 6 | `--resume` drags context through tests → cover → build → fix (120K → 245K) | build/fix — a new session with a short handoff: diff + red gate + error tail | `forge_run.py` | −5–10% |
| 7 | `dotnet restore` on every worktree — 2.5 h of 28.6 | Shared NuGet cache (`NUGET_PACKAGES`) / restore once in the int-worktree and hardlink `obj` | `ppar.py` / `setup` in `powerpuff.yaml` | −9% time |
| 8 | Parallelism of 4 hits the 5-hour limit: effectively ~2.5 units at once | First items 1–6 (fewer tokens → the limit lasts longer); then auto-throttle: lower MAXPAR on `allowed_warning` | `ppar.py` | steady pace |
| 9 | ppar.py is a homegrown wrapper in `vetal_project/.powerpuff/`, not in the plugin | Move parallel waves (int-branch, merge, MAXPAR) into the plugin (`queue_run.py --parallel N`) | plugin | one integration for all projects |
| 10 | Headless sessions inherit the entire user environment (11 plugins, 3 MCP, mem0/telemetry hooks) — the system part is only ~12K, but extra hooks and `ToolSearch` | `--strict-mcp-config` without MCP, `POWERPUFF_DRIVER=1` → silence the mem0/telemetry hooks | `forge_run.py:397` | minor, hygiene |

Expected in total: ≈ −60% tokens. Speed — minus 6 h of pauses and ~2.5 h of restore, plus each call is faster.

## Why it is slow (short)
- Hitting the 5-hour limit: 21% of unit time is pauses, two windows of 1–2 h with no progress at all.
- Abandoned and repeated runs: 42 extra runs on top of 39 merged units.
- Every session drags ~100K of excess context → calls are slower.
- Heavy .NET gates: restore, build `-warnaserror`, tests with coverage on every unit.
- Pace: 39 units in 11.5 h ≈ 3.4 units per hour; 25 left.

## Restarting vetal_project after the refactor
- Stop the current ppar (PID 28396 `ppar.py queue.yaml 4` and child `queue_run.py`) — or wait until it finishes by itself.
- Base — `ppar/integration` (int-worktree `.powerpuff/ppar/int`); skip merged units per `ppar-main.log` / state.
- First finish the running ones (U33, U39, U42, U53) and U01, then the 20 not started.
- Check that `powerpuff.yaml` is compatible with the new plugin version (gates, `test_gates`, protected).
- Update the plugin cache: `C:/Users/suhor/.claude/plugins/cache/powerpuff/powerpuff/<ver>` (ppar.py hardcoded `0.2.0` in PLUGIN).

## Analysis scripts: `C:/Work/notes/powerpuff-usage-scripts/`
`agg.py` (usage by transcripts), `stages.py`/`stages2.py` (by stage from logs), `time.py` (time by phase) — move the logic to `powerpuff/scripts/run_report.py` if needed.

## Outcome (0.3.0, 2026-11-22)
| # | Status |
|---|---|
| 1 | done: `scripts/spec_slice.py`, on vetal 211K → ~14K characters per unit (average over 64 units); the coverage check asks for the unit id only |
| 2 | done; the cause turned out to be different: the message "You've hit your 5-hour limit" was classified as "API unreachable" (24 pauses `FORGE: paused (API unreachable)`); now `rate_limit_event` `rejected` → wait for `resetsAt` and the same stage |
| 3 | done: `max_attempts` (2), retry with the reason and `disputes.md`; also retry on merge conflict |
| 4 | done: `stuck:` → `needs-answer`, continue from `plain-build` |
| 5 | done: `model_build` / `model_fix` (queue, profile, CLI); the last fix round is escalated to `model` |
| 6 | done: build — new session; fix continues build, on model change — a new session with a short handoff |
| 7 | replaced: restore took ~4 s per unit (10 min per night), the 9% "setup" is the base check of all gates; now a cache of green trees (`verified_trees`), setup still runs; in vetal the duplicate `tests` gate was removed (`coverage` covers it) |
| 8 | done: slots by 5-hour window load (`<run>/rate.json`) |
| 9 | done: `queue_run.py --parallel N` / `parallel`, `already_merged`, `budget.unit_usd`; `ppar.py` is no longer needed |
| 10 | partial: `--strict-mcp-config`; the user's mem0/telemetry hooks untouched (they must be silenced on `POWERPUFF_DRIVER=1` in the hooks themselves) |
