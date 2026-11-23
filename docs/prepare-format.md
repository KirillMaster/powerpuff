# prepare — spec preparation and `prepare_run.py`

`prepare_run.py` is the script orchestrator for preparing a spec with a human nearby. The result is a `queue.yaml` for `queue_run.py` (`docs/queue-format.md`), which an autonomous run executes without guessing. The model only proposes: all its sessions have read-only tools (`Read,Grep,Glob`); the script writes the files and the script validates them.

```
python scripts/prepare_run.py <spec dir | spec file | "idea text"> [--repo .] [--model opus] [--out queue.yaml]
       [--max-unit-lines 200] [--max-usd 5] [--no-wait] [--answers questions.json] [--assume-defaults]
       [--accept-lint] [--skip-base-check] [--dry-run]
```

## Flags
| Flag | Default | Meaning |
|---|---|---|
| `--model` | `opus` | model for the questions and slicing sessions; ends up in the queue's `model` |
| `--out` | `queue.yaml` | where to write the queue (relative to the current directory) |
| `--max-unit-lines` | 200 | limit on a unit's `est_lines` |
| `--max-usd` | 5 | cumulative notional preparation budget; checked before every session |
| `--no-wait` | off | subscription limit -> exit 3 instead of waiting |
| `--answers` | none | `questions.json` with filled-in `answer`; answers are matched by `id` |
| `--assume-defaults` | off | unanswered `blocking` questions are resolved with the audit's defaults (they go into slicing marked `[blocking, assumed]`) |
| `--accept-lint` | off | continue despite `spec_lint` errors; findings go into the queue header |
| `--skip-base-check` | off | do not check the base with the gates |
| `--dry-run` | off | only `lint` and `base`, no state is written |
| `--req-regex` | `\b(?:FR\|NFR\|AS\|US)-\d+\b\|\bR\d+\b` | which spec requirement ids must be covered by units |
| `--queue-max-usd` / `--queue-max-hours` | 60 / 8 | `budget` of the emitted queue |
| `--gate-timeout` | 1800 | seconds per gate during the base check |

## Stages
| Stage | What happens | Stop |
|---|---|---|
| `lint` | spec directory -> `spec_lint.lint`; an idea and a file are skipped with a note | errors -> exit 1, findings in the output and `lint.json` |
| `base` | `powerpuff.yaml` profile: all gates on the untouched base; without a profile or without `approved: true` — the line `gates: no approved profile` | red base or missing tool -> exit 1 with gate names, no spend |
| `questions` | one session auditing the spec against the repository: Q-lines (`Q<n> [blocking\|non-blocking\|info]: ... \| default: ... \| cost of a wrong guess: ...`) and D-lines; parsed into `questions.json` (forge format) and `decisions.md` | unanswered `blocking` -> table and exit 4 |
| `slice` | one session -> draft `units.yaml`; checked by the script; problems -> a fix session with the exact list, up to 2 rounds | still invalid -> exit 1, list of problems, the draft stays |
| `emit` | `--out`; runs `queue_run.py --dry-run`, prints the execution order | validator rejected -> exit 1 |

A re-run resumes from where it stopped. The questions session is not run again while `questions.json` exists; to start over, delete the state directory.

## Deterministic draft check
All problems are collected into a single list:
| Check | Source |
|---|---|
| schema, unique ids, dependencies exist, no cycles, a shared file without a dependency between units, the `spec` text | `queue_run.normalize` validator |
| `risk` is set (`low`/`high`), `est_lines` is a number and not above `--max-unit-lines` | script |
| every spec requirement id (`--req-regex`; for a directory — only `spec.yaml`/`spec.md`) is assigned to at least one unit | script |
| the file's directory exists in the repository or is created by this unit or a unit in its dependency chain | script |

Choosing `risk: high` for units touched by a blocking question relies on the prompt; the script does not check it.

## Questions and answers
Exit 4: `PREPARE: needs answers <path to questions.json>`. The human fills in `answer` in `questions.json` (or `--answers file`, or `--assume-defaults`) and repeats the command. Answers go to slicing in a `Human decisions (binding)` block, defaults in a `Defaults taken` block, the audit's D-lines in a `Default decisions of the audit` block.

## State `<repo>/.powerpuff/prepare/<slug>/` (the directory is in `.git/info/exclude`)
| File | Contents |
|---|---|
| `state.json` | stages, cost (`total_usd`, by model and stage, sessions), number of fix rounds |
| `spec.md` | copy of the spec (directory — a concatenation of `spec/plan/tasks/...`; idea — its text) |
| `lint.json` | `spec_lint` result |
| `questions.json`, `decisions.md` | questions with answers; D-lines |
| `units.yaml`, `slice.raw.txt` | draft units (JSON without PyYAML); raw slicing response |
| `progress.md` | log |

`slug`: the name of the spec directory or file; for an idea `idea-<sha1[:8]>`.

## Result
`queue.yaml`: `run_id` = `<date>-<slug>`, `base` = current branch, `spec`, `budget`, `model`, `auto_merge: [low]`, `final.commands` from the profile's `final` section (if any), units `{id, title, deps, risk, requirements, files, spec}`. Header comment:
```
# units: 2 (low 1, high 1)
# requirements covered: 4/4
# questions: answered 1, defaulted 1
# lint accepted: 1 error(s) (--accept-lint)      (only with --accept-lint)
# start: python .../queue_run.py <out> --repo <repo>
```
Without PyYAML the file is written as JSON without the header (the header is printed).

## Exit codes and last line
| Code | Line | Condition |
|---|---|---|
| 0 | `PREPARE: ready <path>` | the queue is written and accepted by the validator |
| 0 | `PREPARE: dry-run ok` | `--dry-run` |
| 1 | `PREPARE: failed (<reason>)` | linter errors, red base, budget, invalid draft, validator rejection, not a git repository |
| 3 | `PREPARE: rate limited (rerun to resume)` | subscription limit with `--no-wait` |
| 4 | `PREPARE: needs answers <path>` | there are unanswered `blocking` questions |
