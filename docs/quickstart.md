# Quick start

Goal: install the plugin, take one spec through `forge` (the default `plain` engine), then the prepare → run → audit loop. The step 3 measurement is in the "What was verified" section.

## Requirements
| What | Condition |
|---|---|
| Python | 3.10+ (the scripts use `X \| None` annotations) |
| git | the project repository, a clean tracked tree before `queue_run.py` |
| `claude` CLI | in PATH, logged in; the drivers run `claude -p` |
| PyYAML | optional; without it `queue.yaml` is written as JSON and `powerpuff.yaml` is read by the built-in parser (two levels, `[a, b]` lists) |
| Project tools | everything the profile commands call (linter, tests); missing tool = gate failed |

## 1. Installation
In Claude Code:
```
/plugin marketplace add KirillMaster/powerpuff
/plugin install powerpuff@powerpuff
```
Commands: `/powerpuff:prepare`, `/powerpuff:queue`, `/powerpuff:forge`. The plugin hooks are registered but do nothing until `powerpuff.yaml` has a `hooks:` section.

## 2. Profile for a small repository
`powerpuff.yaml` at the root; an example for a Python project:
```yaml
approved: true
gates:
  iteration: [lint, tests]
commands:
  lint: "ruff check ."
  tests: "pytest -q"
format_fix: "ruff format ."
protected: [powerpuff.yaml, pyproject.toml]
```
| Rule | Consequence |
|---|---|
| A gate in `gates` without a command in `commands` | `FORGE: failed` before the first session |
| Test gates — the `test_gates` key, otherwise ids with `test` in the name | they are used for the check "acceptance tests fail before implementation" |
| `approved: true` not set | the profile works, the report says `profile not approved` |
| No profile | gates come from the stack preset, the draft is `.forge/powerpuff.draft.yaml`; gates that are red on the untouched base (for example, no coverage tool) are excluded and listed in the report; to approve = save as `powerpuff.yaml` with `approved: true` |

Commit the profile: before the run all gates pass once on the untouched base; with an approved profile a red base = refusal (`--allow-red-base` bypasses it deliberately).

## 3. One spec through forge
A spec is a directory with `spec.md` (or `spec.yaml`, `plan.md`, `tasks.md`); it is convenient to number requirements `R1`, `R2`…
```
/powerpuff:forge specs/001-feature
```
Or directly:
```
python <plugin>/scripts/forge_run.py specs/001-feature --repo . --max-usd 20
```
Flow of the `plain` engine: Opus asks questions about ambiguities and writes acceptance tests → the script freezes them and checks that they are red on the base → the same session writes the code → the script runs the gates and returns anything red to the same session (up to 4 rounds). Details — `docs/engine-plain.md`; the former multi-session pipeline is `--engine forge`.
| What | Where |
|---|---|
| Progress | `.forge/progress.md` |
| Requirements, decisions, questions | `.forge/requirements.md`, `.forge/questions.json` |
| Result | `.forge/REPORT.md`, the last output line `FORGE: done` or `FORGE: failed` |
| State | `.forge/state.json`; a re-run resumes from where it stopped |

Unanswered blocking questions stop the run (`FORGE: failed (blocking questions: …)`, code 1): answer in the `answer` field in `.forge/questions.json` and restart — the session continues; `--assume-defaults` takes the default answers right away.

| Code | Action |
|---|---|
| 0 | `done`: read `.forge/REPORT.md`, the decisions section `D`, the `gates:` and `guard:` lines |
| 1 | the reason is in `REPORT.md` and the last line: `base is red: <gates>` — fix the base; `blocking questions: Q1,Q4` — answer in `questions.json` and repeat, or `--assume-defaults`; `cost budget exhausted` / `wall-clock budget exhausted` — raise `--max-usd` / `--max-hours` and repeat; anything else — edit the spec |
| 3 | subscription limit with `--no-wait`: repeat after the reset, the run continues |

## 4. Spec preparation
```
/powerpuff:prepare specs/001-feature --out queue.yaml
```
Stages: `spec_lint` → gates on the base → questions → slicing into units → `queue.yaml`. State — `.powerpuff/prepare/<slug>/` (`lint.json`, `questions.json`, `decisions.md`, `units.yaml`, `progress.md`). Last line: `PREPARE: ready queue.yaml`.

| Code | Line | Action |
|---|---|---|
| 0 | `PREPARE: ready <path>` | check the `queue.yaml` header (number of units, requirement coverage), start the queue |
| 1 | `PREPARE: failed (<reason>)` | linter errors — fix the spec (`--accept-lint` only deliberately); red base — fix it; invalid slicing — the list of problems is in the output, the draft stays |
| 3 | `PREPARE: rate limited` | repeat the same command after the reset |
| 4 | `PREPARE: needs answers <questions.json>` | fill in `answer` for the blocking questions (or `--answers file`, or `--assume-defaults`), repeat the command |

Free check: `--dry-run` runs only the linter and the base gates.

## 5. Autonomous run
```
/powerpuff:queue queue.yaml --dry-run
/powerpuff:queue queue.yaml
```
`--dry-run` prints the unit order and the budget. The run goes in a separate process; log — `.queue-run.log`, per unit — `.powerpuff/runs/<run_id>/<id>.log`.

| File in `.powerpuff/runs/<run_id>/` | Contents |
|---|---|
| `state.json` | status, branch, cost of each unit |
| `SUMMARY.md`, `summary.json` | totals, those awaiting audit, blocked ones, final contour |
| `forge/<id>/` | copy of the unit's `.forge` |

| Code | Last line | Action |
|---|---|---|
| 0 | `QUEUE: done` | go to the audit |
| 1 | `QUEUE: failed` | no unit finished; reasons in `SUMMARY.md` and the unit logs |
| 2 | `QUEUE: partial` | part done; failures and blocks go to the audit |
| 3 | `QUEUE: config error` | list of problems in the output, nothing created; or the limit with `--no-wait` — repeat after the reset |
| 4 | `QUEUE: paused after <id>` | `--attended` only; repeat the command for the next unit |

Units whose `risk` is not in `auto_merge` stay `done` until the audit. A `needs-answer` unit continues after the answer in `.forge/questions.json` (the path is in the reason) and a re-run.

## 6. Audit
```
python <plugin>/scripts/audit_cards.py .powerpuff/runs/<run_id>
```
The result is `.powerpuff/runs/<run_id>/cards.html` (a single file, opens from `file://`). Decisions are downloaded with the "Export" button as `answers.yaml`.
```
python <plugin>/scripts/audit_apply.py answers.yaml --run .powerpuff/runs/<run_id> --dry-run
python <plugin>/scripts/audit_apply.py answers.yaml --run .powerpuff/runs/<run_id>
```
Result: `next-queue.yaml` in the run directory, `decisions.md` and `audit-actions.md`. The next run:
```
/powerpuff:queue .powerpuff/runs/<run_id>/next-queue.yaml
```
| `audit_cards.py` code | Action |
|---|---|
| 0 | `AUDIT: N cards (M to decide) -> <path>` |
| 3 | `summary.json` is missing or unreadable — finish the run first |

| `audit_apply.py` code | Action |
|---|---|
| 0 | everything done |
| 2 | an action failed (merge conflict, writing the answer): look at `FAILED` in the output, resolve by hand |
| 3 | invalid input (foreign `run_id`, unknown card, invalid decision); nothing changed; re-applying the same answers to questions also gives 3 |

## What was verified
Steps 1–3 were passed on 2026-11-19 on fresh repositories without a profile (marketplace from a local clone): TypeScript without `node_modules` — `FORGE: done` in ~2 min and $0.56, Python — in 88 s and $0.50 (`docs/EFFICIENCY.md`).
Only step 3 on a single spec is confirmed by a real run. Steps 4–6 are covered by tests with a stub `claude`; real queue runs (criteria 4.1, 5.1, 5.3) are open. State — `docs/ROADMAP.md`.

## Next
`docs/prepare-format.md`, `docs/queue-format.md`, `docs/audit-format.md` — full flag tables; `docs/attended-mode.md` — hooks for sessions with a human.
