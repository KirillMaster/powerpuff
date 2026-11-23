# audit — cards, `answers.yaml` and `audit_apply.py`

The audit after an autonomous run: a human goes through `cards.html` (10–20 minutes), exports `answers.yaml`, and the script turns the answers into actions and the queue for the next run. No models are called.

```
python scripts/audit_cards.py <run dir | summary.json> [--repo .] [--out <run dir>/cards.html] [--max-diff-lines 400]
python scripts/audit_apply.py answers.yaml --run <run dir> [--repo .] [--queue queue.yaml] [--out next-queue.yaml] [--dry-run]
```

## Cards (`cards.html`)
A single file: CSS and JS inline, no external requests, opens from `file://`, light and dark theme via `prefers-color-scheme`. The data lives in one JSON block, text from files is rendered via `textContent`.

| Kind | What goes in | Id | Options |
|---|---|---|---|
| `question` | an open question of any unit; `blocking` first, then `non-blocking`; `info` goes in a collapsed block of the `summary` card | `question:<unit>:<Qn>` | `accept-default`, `answer` |
| `high-risk` | a `done` unit awaiting audit: decisions `D`, diff stat, collapsed patch, gates, guard | `unit:<unit>` | `merge`, `reject`, `rework` |
| `failure` | a `failed` unit: reason, start of `REPORT.md`, output of failed checks, end of the log | `unit:<unit>` | `retry`, `rework`, `drop` |
| `blocked` | `blocked`, `skipped-budget`, `needs-answer` | `unit:<unit>` | `requeue`, `drop` |
| `final` | a failed final-contour command with the output tail | `final:<command name>` | `fix`, `ignore` |
| `summary` | units by status, cost by model, time, branch | `summary` | no decision |

The diff is taken as `git diff <integration_branch>...<branch>`; if there is no repository, branch or `.forge` snapshot, the section is skipped with a note and generation does not fail.

Keys: `j`/`k` — next/previous card, `1`–`9` — option, `e` — export. Progress "decided n / m"; decisions are kept in `localStorage` by `run_id`. "Export" downloads `answers.yaml` and shows it in a copy field.

## `answers.yaml`
| Field | Meaning |
|---|---|
| `run_id` | must match the run's `run_id` |
| `decided_at` | export time, ISO 8601 |
| `answers[].card` | card id from the table above |
| `answers[].decision` | `accept-default`, `answer`, `merge`, `reject`, `rework`, `retry`, `drop`, `requeue`, `fix`, `ignore` |
| `answers[].text` | comment or answer; required for `answer` and `rework` |

Undecided cards do not appear in the file. Whether a decision is allowed depends on the card kind (table above); `accept-default` requires a non-empty default value.

```yaml
run_id: "2026-11-17-a"
decided_at: "2026-11-17T10:00:00.000Z"
answers:
  - card: "question:U6:Q1"
    decision: answer
    text: "oauth2"
  - card: "unit:U2"
    decision: rework
    text: "use cursor pagination"
  - card: "unit:U3"
    decision: retry
    text: ""
  - card: "final:lint"
    decision: fix
    text: "only src/a.py"
```

Scalars are in double quotes (JSON escaping). Without PyYAML `audit_apply.py` reads this format and JSON.

## What `audit_apply.py` does
| Decision | Action |
|---|---|
| `accept-default` / `answer` | the `answer` field in the unit working copy's `.forge/questions.json` (if it still exists) and in its snapshot `<run dir>/forge/<unit>/`; an entry in `decisions.md` |
| `merge` | the unit is merged into the integration branch in a temporary worktree (`worktree add --detach`, `merge --no-ff`, `update-ref` with an old-value check); the user's working copy is untouched; branch checked out in another worktree or a conflict — action `FAILED`, exit code 2; already merged — skipped |
| `rework`, `retry`, `requeue` | the unit goes into the next queue |
| `fix` | a new unit `FIX-<name>`: the failed command, output tail, comment |
| `reject`, `drop`, `ignore` | only an entry in `audit-actions.md` |

Next queue (`--out`, default `<run dir>/next-queue.yaml`):
| Field | Value |
|---|---|
| `run_id` | `<run_id>-r2`, `-r3`, … |
| `base` | the run's integration branch |
| `spec`, `budget`, `model`, `writer`, `auto_merge`, `assume_defaults`, `final` | copied from the original queue (`--queue`, otherwise `queue_file` from `state.json`) |
| `units` | the original queue's units in the original order, then `FIX-*`; `spec_file` is embedded into `spec` |
| `units[].deps` | only those present in the new queue; a dependency on an absent unit is replaced by that unit's own dependencies (merged and dropped ones are not required) |
| `units[].spec` | original text + `Human answers:` (answers to this unit's questions) + `Human feedback:` (comment) |

Files in `<run dir>`: `decisions.md` (answers to questions), `audit-actions.md` (git commands of merges and the other decisions), both appended to on each application.

## Exit codes
| Code | Condition |
|---|---|
| 0 | everything done |
| 2 | an action failed (merge conflict, answer write) |
| 3 | invalid input: foreign `run_id`, unknown card, disallowed decision, missing text, no original queue; all problems are listed, nothing is changed |

`--dry-run` validates the input and prints the plan: no files are written, no branches move. Re-applying the same answers to questions gives code 3: closed questions are no longer counted as cards.
