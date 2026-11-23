# powerpuff

A Claude Code plugin for agentic development: generic, works with any stack and project. A human prepares the spec and runs the audit; the code is written in an autonomous run without them.
Rule: **the model proposes, the script decides** — the "done" verdict comes from the exit codes of commands run by the driver, not from the model's words.
The core knows only the "command → exit code" contract; everything language-specific lives in the project profile `powerpuff.yaml`.

**Default mode** (per the bench, `docs/FINDINGS.md` A22): plain Claude Code on Opus, one session per spec slice, with frozen acceptance tests and powerpuff gate scripts; the `forge` orchestrator only on explicit request (same quality, cost ×3–4, ×8 on a multi-stage project).

## Cycle
| Stage | Command | Script | Result |
|---|---|---|---|
| Spec preparation (human present) | `/powerpuff:prepare <spec>` | `prepare_run.py` | `queue.yaml`, questions along the way |
| Autonomous run | `/powerpuff:queue queue.yaml` | `queue_run.py` | a branch and worktree per unit, `SUMMARY.md` |
| One unit (or the whole spec) | `/powerpuff:forge <spec>` | `forge_run.py` | `.forge/REPORT.md`, `FORGE: done\|failed` |
| Audit | `audit_cards.py` → `cards.html` → `answers.yaml` → `audit_apply.py` | both scripts | `next-queue.yaml` for the next run |

An autonomous run can be started at any time; the mode with a human present is `queue_run.py --attended` plus hooks (`docs/attended-mode.md`).

## Components
| Script | Purpose | Exit codes |
|---|---|---|
| `forge_run.py` | one spec; `plain` (default): questions + frozen acceptance tests → code in the same session → gates by script → fix rounds; `--engine forge`: the previous pipeline | 0 done, 1 failed, 3 subscription limit with `--no-wait` |
| `forge_check.py` | "done": requirement coverage, freeze, gates | 0 done, 1 not done |
| `gate_guard.py --base <sha>` | diff-based bypass protection: skipped tests, suppressions, protected files, test count | 0, 1 violations, 3 input error |
| `diff_gates.py <gate> --base <sha>` | diff coverage, duplicates, complexity, size | 0, 1, 3 cannot evaluate (no report = failure) |
| `spec_lint.py <spec dir>` | spec linter | 0, 1 errors, 3 input error |
| `prepare_run.py` | spec → `queue.yaml` | 0, 1 failed, 3 limit, 4 answers needed |
| `queue_run.py` | walks the queue, merges low-risk units, final contour | 0 done, 1 failed, 2 partial, 3 config error or limit, 4 `--attended` pause |
| `audit_cards.py` | `cards.html` from the run directory | 0, 3 input error |
| `audit_apply.py` | `answers.yaml` → actions and `next-queue.yaml` | 0, 2 action failed, 3 invalid input |
| `hook_stop.py`, `hook_protect.py` | Claude Code hooks, opt-in | 0 allow, 2 block |

## Project profile
A human chooses the gates: `powerpuff.yaml` in the repository root. Without it the `plain` engine takes gates from the stack preset (draft `.forge/powerpuff.draft.yaml`, the report is marked "NOT APPROVED"); gates that are red on the untouched base are excluded from the draft and listed in the report.
```yaml
approved: true
gates:
  iteration: [build, lint, tests]
  unit: [diff-coverage, size]
  final: [mutation-diff]
commands:
  build: "cargo build"
  lint: "cargo clippy -- -D warnings"
  tests: "cargo test"
  diff-coverage: "python scripts/diff_gates.py coverage --base {base} --report lcov.info"
  size: "python scripts/diff_gates.py size --base {base} --max-lines 400"
  mutation-diff: "cargo mutants --in-diff"
format_fix: "cargo fmt"
protected: [powerpuff.yaml, rustfmt.toml]
allow_suppressions: []
hooks:
  stop_gates: true
  protect: true
diff_gates:
  coverage: {report: lcov.info, min: 80}
  size: {max_lines: 400}
```
| Key | Meaning |
|---|---|
| `approved` | set by a human; missing or `false` = draft, the report says `profile not approved` |
| `gates` | tiers: `iteration` and `unit` decide "done"; `final` is not yet run by the `forge_run` driver, the queue's heavy contour is set in `final.commands` of `queue.yaml` |
| `commands` | a command for each id; no command or tool — the gate fails |
| `protected`, `allow_suppressions` | files and exceptions for `gate_guard` and `hook_protect` |
| `hooks` | enables the attended-mode hooks; without the section the hooks do nothing |
| `diff_gates` | thresholds for `diff_gates.py all --base <sha> --config powerpuff.yaml` |

More: `docs/design-gates.md`, `docs/diff-gates.md`.

## Stacks
The core does not know the stack. Command presets are starting values, not code: .NET, TypeScript, Python, Rust, Go, and any other via a manual profile; see the table in `docs/design-gates.md` (section "Any stack"), coverage and complexity reports per stack in `docs/diff-gates.md`. The Rust and Go presets were written from the tools' documentation and have not been verified on real runs.

## Efficiency
Report `docs/EFFICIENCY.md` (2026-11-19): on tasks from real repositories powerpuff is no worse than plain Claude Code on any and better on three of six (behavior 0.996 vs 0.968) at cost ×2.2 and time ×2.3. A fresh Python repository without a profile: `FORGE: done` in 88 s and $0.50.

## Status
Plan progress: **14 of 43 criteria (33%)**, details in `docs/ROADMAP.md`. The foundation stages and most of the gates, spec linter and queue mechanics are closed.
`forge_run` (engines `plain` and `forge`) has been verified by real runs on a bench of tasks from real repositories and on a fresh repository without a profile (`docs/EFFICIENCY.md`). The queue went through a real overnight run (64 .NET units, a parallel wrapper around `queue_run`); based on its data, 0.3.0 trims the spec in the unit prompt, waits out the subscription limit without a restart, runs build/fix in separate sessions (can be Sonnet), and the queue itself runs units in parallel (`--parallel N`) with retries of failures (`docs/HANDOFF-2026-11-22-cost-refactor.md`, `CHANGELOG.md`). Spec preparation, audit cards and hooks are covered by tests with a stub `claude`; the pilots (stage 7) have not started. The recommended entry point is a single spec via `/powerpuff:forge`.

## Requirements
Python 3.10+, git, `claude` CLI in PATH; PyYAML is optional (without it `queue.yaml` is written as JSON). Step by step: `docs/quickstart.md`.

## Documents
| Document | About |
|---|---|
| `docs/quickstart.md` | installation and the first cycle |
| `docs/migration-from-bob-pipeline.md` | migrating from bob-pipeline |
| `docs/ROADMAP.md`, `docs/FINDINGS.md` | plan with criteria, findings log |
| `docs/design-system.md`, `docs/design-forge.md`, `docs/design-gates.md` | design |
| `docs/prepare-format.md`, `docs/queue-format.md`, `docs/audit-format.md` | formats and exit codes |
| `docs/diff-gates.md`, `docs/attended-mode.md` | diff gates, hooks |
| `docs/from-bob-pipeline.md` | what was taken from bob-pipeline and what was not |
| `docs/research/` | research with sources |
