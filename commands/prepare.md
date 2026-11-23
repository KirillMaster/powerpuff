---
description: Spec preparation — an idea, a file or a spec directory -> queue.yaml for an autonomous run. The script orchestrates, the model only proposes; questions to the human along the way
argument-hint: "<spec-dir | spec file | \"idea text\"> [--out queue.yaml] [--max-unit-lines N] [--max-usd N] [--accept-lint] [--assume-defaults] [--dry-run]"
---

# /powerpuff:prepare — spec preparation

The orchestrator is the script `${CLAUDE_PLUGIN_ROOT}/scripts/prepare_run.py`, not you. Do not implement or slice anything yourself. Format: `docs/prepare-format.md` in the plugin.

1. `<root>` = `git rev-parse --show-toplevel`.
2. In the FOREGROUND, from `<root>`, with `$ARGUMENTS` unchanged: `python ${CLAUDE_PLUGIN_ROOT}/scripts/prepare_run.py $ARGUMENTS --repo <root>`. Quote idea text that contains spaces.
3. Exit code, by the last line of output:
   | Code | Last line | Action |
   |---|---|---|
   | 0 | `PREPARE: ready <path>` | show the path and the summary from the `queue.yaml` header (units, low/high, requirement coverage, questions); suggest `/powerpuff:queue <path>` |
   | 1 | `PREPARE: failed (<reason>)` | show the reason and the list of problems (linter errors, red gates, draft problems); fix the spec or flags, do not work around |
   | 3 | `PREPARE: rate limited` | report it; repeating the same command resumes where it left off |
   | 4 | `PREPARE: needs answers <questions.json>` | step 4 |
4. Code 4: show the human a table of blocking questions (question, default decision, cost of wrong guess), collect the answers, write each into the `"answer"` field of the matching `Q` in `<root>/<questions.json>` and repeat the same command. The audit is not re-run; the answers go to slicing as mandatory decisions. "Accept default" = `--assume-defaults`.
5. Use `--accept-lint` only on an explicit human decision; accepted findings go into the `queue.yaml` header.

Stages (state in `<root>/.powerpuff/prepare/<slug>/state.json`, a repeat resumes where it left off):
| Stage | Who | Result |
|---|---|---|
| `lint` | script | `spec_lint`; errors -> exit 1, `lint.json` |
| `base` | script | `powerpuff.yaml` gates on the untouched base; a red one -> exit 1 at no cost |
| `questions` | `--model`, read-only | `questions.json` (Q lines), `decisions.md` (D lines) |
| `slice` | `--model`, read-only | `units.yaml`; checked by the script, up to 2 fix rounds |
| `emit` | script | `--out`, validated by `queue_run.py --dry-run` |
