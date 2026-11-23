# Migrating from bob-pipeline

The rationale for what was kept and what was not is in `docs/from-bob-pipeline.md`; this page gives the mapping and the order of steps. The migration was done on 2026-11-20 on personal and work repositories (`powerpuff.yaml` profiles written per this guide); a real `/powerpuff:forge` run on each of them is the next step (criterion 8.2).

## Concept mapping
| bob-pipeline | powerpuff | Note |
|---|---|---|
| `/bob-run <feature>` | `/powerpuff:forge <spec>` for a single spec, `/powerpuff:queue queue.yaml` for several units | the orchestrator is a script, not a model |
| Slice | unit in `queue.yaml` (`id`, `deps`, `risk`, `requirements`, `files`, `spec`) | target: a diff of up to ~200 lines; slicing is checked by a script |
| Specifier (Gherkin, QA procedures, slices) | yamlkit spec (`spec.yaml`, ids `AS-n`/`AC-n`/`FR-n`) + `/powerpuff:prepare`: `spec_lint.py` → questions → slicing → `queue.yaml` | Gherkin as a mandatory layer is not carried over |
| Scout | not needed: a single session reads the repository itself | |
| Coder | `plain` engine (default): one Opus session — questions and acceptance tests for every spec id, freeze, then code in the same session; the script runs the gates | `docs/engine-plain.md` |
| QA | profile gates + a check of the frozen tests hidden from the model; fresh-context acceptance is the `--accept` option | by measurement, acceptance added no quality |
| Architect, Hardener | not carried over | see below |
| `.claude/bob-pipeline.yaml` | `powerpuff.yaml` at the repository root | different format, see step 2 |
| Gates E/G (format, linter, architecture, tests) | `gates.iteration` and `gates.unit` + profile `commands`; the command's exit code decides | a missing tool = failure |
| Ban on touching tests and weakening gates | frozen hashes + `gate_guard.py` + rollback of `powerpuff.yaml` edits during a run | plus `hook_protect.py` in sessions with a human |
| Diff gates (complexity, duplicates) | `diff_gates.py`: `coverage`, `duplication`, `complexity`, `size` | profile commands with exit code 0/1/3 |
| K2 (red test on a clean base) | the check "acceptance tests fail before implementation" in `forge_run.py` | |
| K3, K4, `ci_gates.py` | not carried over | candidates: one at a time and only with a measured benefit |
| Worktree per slice (`.worktrees/bob-*`) | worktree per unit `.powerpuff/runs/<run_id>/wt/<id>`, branch `powerpuff/<run_id>/<id>` | low-risk merge goes into the integration branch without touching the working copy |
| Resuming after a failure, waiting out the limit, budget | same: a re-run resumes; `--max-usd`, `--max-hours`, a third of the budget reserved for fixes | |
| Run report (`run-report.md`) | `.forge/REPORT.md` per unit, `SUMMARY.md` and `summary.json` per queue, `cards.html` for the audit | |
| Modes without a human and with a human | autonomous run and attended mode (`--attended`, hooks) | not tied to the time of day |

## Migrating a project step by step
| No. | Action | Check |
|---|---|---|
| 1 | Install the plugin (`docs/quickstart.md`) | `/powerpuff:forge` is available |
| 2 | Write `powerpuff.yaml` (draft: `python <plugin>/scripts/profile_init.py`) from the commands bob-pipeline ran as gates; key `setup` — dependency installation; tier `iteration` — fast ones (build, format, linter, quick tests), `unit` — slow ones (diff coverage, architecture) | on a clean base all commands exit with 0 |
| 3 | Move the list of protected files to `protected` (linter and analyzer configs, `powerpuff.yaml` itself); justified suppressions go to `allow_suppressions` | `gate_guard.py --base <sha>` reports no violations on the current tree |
| 4 | Set `approved: true` | `REPORT.md` contains the line `gates: approved profile` |
| 5 | Leave the yamlkit-format spec (`spec.yaml`/`tasks.yaml`) as is; Gherkin and QA procedures are not needed for powerpuff | `spec_lint.py <spec directory>` exits with 0 or the list of fixes is clear |
| 6 | Run one small feature: `/powerpuff:forge <spec>` | `FORGE: done`, your own tests and linter pass on the result |
| 7 | For a set of independent stages: `/powerpuff:prepare <spec>` → `queue.yaml`, `/powerpuff:queue queue.yaml --dry-run`, then the run | `PREPARE: ready`, `QUEUE: dry-run ok` |
| 8 | Instead of reading role reports — the audit: `audit_cards.py`, `answers.yaml`, `audit_apply.py` | `queue_run.py --dry-run` accepts `next-queue.yaml` |
| 9 | Enable hooks for work with a human: the `hooks:` section in the profile (`stop_gates`, `protect`) | `docs/attended-mode.md` |

powerpuff neither reads nor changes bob-pipeline state (`.bob/`, `.claude/agents/bob/`, `.worktrees/bob-*`). Finish or reset unfinished bob-pipeline slices with bob-pipeline itself before migrating.

## What was intentionally not carried over
| What | Why |
|---|---|
| Five roles on every slice | on the benchmark pilot quality was 0.70–0.78 vs 0.94–1.0 for plain Claude Code at 5–20 times the cost (`docs/FINDINGS.md`, A1, A3) |
| Gherkin and QA procedures as a mandatory layer | duplicate the spec, showed no benefit in measurement |
| Orchestrator model | on a real spec cost $11.42; a script orchestrator with a single writer — $8.09 (A4, A5) |
| Mutation testing in the loop | no data on benefit in an agent loop, expensive; mutations on the diff — only as the `final` gate |
| Run metrics in their old form | simplified to cost by model and stage in `state.json` |
| Gates K3, K4, `ci_gates.py` | not measured separately; ported one at a time with measurement |

Comparison on a shared task set: `docs/EFFICIENCY.md` and `docs/FINDINGS.md` A22–A23 — the role-based pipeline is worse than plain Claude Code (Δ −0.28), powerpuff `plain` is no worse than it on any task at ×2.2 the cost.

## Coexistence
| Question | Answer |
|---|---|
| Can both plugins be installed | yes; commands live in different namespaces (`/bob-run`, `/powerpuff:*`), state in different directories (`.bob/`, `.worktrees/bob-*` vs `.powerpuff/`, `.forge/`) |
| powerpuff hooks | opt-in: without `powerpuff.yaml` or without a `hooks:` section the hook exits with 0 and does nothing |
| bob-pipeline config | `forge_run.py` does not read `.claude/bob-pipeline.yaml`; it is only mentioned in the audit prompt as a hint for the model when drafting gates if `powerpuff.yaml` is absent. An approved profile disables the hint |
| One repository, both pipelines | do not run them simultaneously in one working copy; the directories `.powerpuff/`, `.forge/` and `.forge-unit-spec.md` are added to `.git/info/exclude` |
