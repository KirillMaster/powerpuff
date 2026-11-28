# queue.yaml — the unit queue and `queue_run.py`

`queue_run.py` is the external script orchestrator of an autonomous run: it calls no models itself and hands each unit to `forge_run.py`. The failure or blocking of one unit does not stop the run.

```
python scripts/queue_run.py queue.yaml [--repo .] [--engine plain|forge] [--no-wait] [--only U1,U2] [--dry-run] [--attended]
```

## Example
```yaml
run_id: 2026-11-17-a
base: main
integration_branch: powerpuff/run-2026-11-17-a
spec: specs/001-feature
budget: {max_usd: 60, max_hours: 8, fix_reserve: 0.33}
model: opus
writer: sonnet
engine: plain          # plain (default) | forge; CLI --engine overrides
auto_merge: [low]
final:
  timeout: 1800
  commands:
    unit_all: "pytest -q"
    smoke: "python scripts/smoke.py"
units:
  - id: U1
    title: "Header parser"
    deps: []
    risk: low
    requirements: [FR-1, AS-2]
    files: [src/parser.py]
    spec: |
      What to build: ...
  - id: U2
    title: "/items endpoint"
    deps: [U1]
    risk: high
    files: [src/parser.py, src/api.py]
    spec_file: units/u2.md
```

## Fields
| Field | Default | Meaning |
|---|---|---|
| `run_id` | file modification date + 6 characters of sha1 | run name; to resume after a day or after editing the file, set it explicitly |
| `base` | `HEAD` | branch or commit the run starts from |
| `integration_branch` | `powerpuff/run-<run_id>` | the branch units are merged into; cannot equal `powerpuff/<run_id>` |
| `spec` | none | shared spec (directory or file). Only its slice goes into a unit's spec (`scripts/spec_slice.py`): yamlkit elements with ids from `requirements` and from the unit's text (FR/AS/AC/T/C/E/US), `tasks.yaml` tasks with those `requirement_refs`, their contracts and entities; for markdown — paragraphs with those ids. A spec ≤ 12K characters is inserted whole. The rest is a list of paths for Read |
| `budget.max_usd` / `max_hours` | 60 / 8 | limits for the whole run |
| `budget.fix_reserve` | 0.33 | share of the budget outside the first pass (0 ≤ x < 1) |
| `budget.unit_usd` | none | explicit per-unit limit instead of the formula below |
| `model` / `writer` | `opus` / `sonnet` | strong model and writer for `forge_run.py` |
| `model_build` / `model_fix` | `model` / `model_build` | plain engine: model for the implementation turn and for fix rounds (`--model-build` / `--model-fix`); the last fix round is escalated to `model`. Without the key — `model_build`/`model_fix` from `powerpuff.yaml` |
| `advisor` | none (profile `advisor`) | plain engine: advisor model of build/fix sessions (`--advisor`); `off` disables a profile advisor |
| `parallel` | 3 | how many independent units run at once (`--parallel N` overrides; env `POWERPUFF_PARALLEL` changes the default; deps, shared files and the rate throttle still gate it) |
| `worktree_pool` | on | `off` (or env `POWERPUFF_POOL=0`, or profile `worktree_pool: off`) removes finished unit worktrees instead of keeping them in `<repo>/.powerpuff/pool/` (max 6) to reuse their `bin/` / `obj/` build outputs |
| `max_attempts` | 2 | total runs per unit; the next one starts from a fresh branch, with the reason for the previous failure and its `disputes.md` in the spec |
| `already_merged` | `[]` | ids of units already merged into `base` (continuing a previous run): immediately `merged` |
| `auto_merge` | `[]` | risk levels that merge without a human |
| `assume_defaults` | `false` | `true` → `--assume-defaults` to `forge_run.py`: unanswered `blocking` questions are resolved with the auditor's defaults; non-bool = configuration error |
| `final.commands` | none | `{name: command}`, the heavy contour in the integration worktree after the queue; if the repository has `powerpuff.yaml`, the contour = the gates of its `final` tier + these commands (same-named ones from the queue override). Substitutions `{base}` (the run's `base_sha`) and `{root}`. The source is `final_gates_source` in `summary.json`, an unapproved profile is marked. Missing tool = failure |
| `final.timeout` | 1800 | seconds per command |
| `units[].id` | required | unique, `[A-Za-z0-9_.-]+` |
| `units[].title` | `id` | title |
| `units[].deps` | `[]` | ids of units that must be merged into the integration branch |
| `units[].risk` | `high` | `low` or `high` |
| `units[].requirements` | `[]` | requirement ids; informational and go into the unit's spec |
| `units[].files` | `[]` | the unit's files; a shared file without a dependency between units = configuration error |
| `units[].spec` / `spec_file` | one of the two is required | the unit's text; `spec_file` — a path next to `queue.yaml` |

Without PyYAML the file is read as JSON.

## Pre-start validation (exit 3, all problems as a list, nothing is created)
Duplicate id, unknown dependency, cycle, invalid `risk`, no spec text, overlapping files, dirty tree (changes to tracked files), no `base`, occupied integration branch.

## Run flow
| Step | What happens |
|---|---|
| Selection | the first `pending` unit whose `deps` are all `merged`; one at a time, with `parallel: N` — up to N at once. Slots by account limit (the sessions' last `rate_limit_event`, `<run>/rate.json`): 5-hour window load ≥ 75 % → N/2, ≥ 90 % or `allowed_warning` → 1, window `rejected` → no new units start until `resetsAt` |
| Branch | `powerpuff/<run_id>/<id>` from the current tip of the integration branch, worktree `.powerpuff/runs/<run_id>/wt/<id>` |
| Launch | `forge_run.py <wt>/.forge-unit-spec.md --repo <wt> ... --no-wait --stream`, output to `<run>/<id>.log` |
| Code 0 | `done`; risk in `auto_merge` → `git merge --no-ff` in the integration worktree → `merged`; conflict → `failed` ("merge conflict"), the branch stays |
| Code 0, risk not in `auto_merge` | stays `done` until the audit; dependents → `blocked` ("awaits audit of Ux") |
| Code 1 | an attempt remains (`max_attempts`) → copy of `.forge` to `forge/<id>.attemptN`, worktree and branch are deleted, a new attempt from the current tip; otherwise `failed` (`... (after N attempts; needs a human)`) |
| Code 1, `stuck:` | the fix round changed nothing: `needs-answer` without retry, reason + the start of `.forge/disputes.md`; the decision goes into `.forge/questions.json` (`answer`), a re-run continues the rounds in the same worktree |
| Code 1, blocking questions | `needs-answer`, reason `<forge reason>; answer in <wt>/.forge/questions.json and rerun`; dependents → `blocked` ("dep Ux needs-answer") |
| Code 3 | wait for the limit reset (`resetsAt` from `rate_limit_event`, otherwise the time from the text; no longer than the remaining hours) and repeat the same stage in the same worktree; with `--no-wait` the run ends with code 3, state saved |
| Code 3, `FORGE: paused (API unreachable)` | not a failure and not a crash: a pause with growing wait 30 s … 15 min and a retry of the same unit (in total no longer than `--max-outage-min`, 120, and the remaining hours); with `--no-wait` or after the limit the run ends with code 3, the unit stays `running` (reason `API unreachable`), a re-run continues from the same stage |
| Other code | one retry (forge continues from where it stopped), then `failed` |
| Dependency `failed`/`blocked`/`skipped-budget`/`done` | the unit is `blocked`, independent ones continue |

The user's working copy is not touched: merges go into a separate worktree `wt/_integration`, HEAD does not move. Worktrees of merged units are deleted (branches and the `.forge` copy stay), the rest wait for the audit.

## Budget
- Unit limit: `budget.unit_usd`, otherwise `min(remainder, max_usd * (1 - fix_reserve) / number_of_units * 2)`.
- Base check: a tree on which the gates were already green (the previous unit's base or the result of a `done` unit) is not checked a second time — `forge_run.py --skip-base-check` (setup still runs). The list is `verified_trees` in `state.json`.
- Remainder below the limit or hours exhausted → the remaining units are `skipped-budget`.
- Cost is taken from each unit's `.forge/state.json`.
- A re-run returns `needs-answer`, `blocked` and `skipped-budget` to `pending`; a `needs-answer` unit continues in the same worktree (the audit is not repeated) after `answer` is filled in `.forge/questions.json`; a `done` unit already merged by hand is marked `merged`.

## State and result
| File (`<repo>/.powerpuff/runs/<run_id>/`) | Contents |
|---|---|
| `state.json` | per unit: `status` (pending, running, done, merged, failed, needs-answer, blocked, skipped-budget), `branch`, `worktree`, `forge_status`, `cost`, `started`, `finished`, `reason`; totals: cost by model, time |
| `<id>.log` | `forge_run.py` output |
| `forge/<id>/` | copy of the unit's `.forge` (report, requirements, state, check) |
| `SUMMARY.md` | table of units, totals, those awaiting audit, blocked ones, heavy contour |
| `summary.json` | the same, machine-readable; key `needs_answer`: `[{id, reason}]` (in `SUMMARY.md` — the section `## Needs answer`); per unit: path to `REPORT.md`, `D`/`Q` lines, open gaps, guard result |

`.powerpuff/`, `.forge/` and `.forge-unit-spec.md` are added to `.git/info/exclude`.

## Exit codes and last line
| Code | Line | Condition |
|---|---|---|
| 0 | `QUEUE: done` | all units `merged` or `done`, heavy contour green |
| 2 | `QUEUE: partial` | some `merged`/`done`, but not all, or the heavy contour failed |
| 1 | `QUEUE: failed` | no `merged`/`done` at all |
| 3 | `QUEUE: config error` / no line | configuration error; limit with `--no-wait`; API unavailable (`--no-wait` or `--max-outage-min` exhausted) |
| 4 | `QUEUE: paused after <id>` | `--attended`, units remain |

The heavy contour runs once for a given integration branch tip and only if at least one unit is merged; a failure does not change unit statuses.
