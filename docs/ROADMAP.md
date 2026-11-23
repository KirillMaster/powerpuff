# powerpuff development plan and progress scale

Plan rules:
- A stage is closed only when **all** its criteria are met; each criterion is verified by a command or an artifact, not by opinion.
- A component stays only if its value shows up in a measurement (bench or pilot). Otherwise it is removed.
- Findings and rationale are in `docs/FINDINGS.md`. Terms: spec preparation → autonomous mode → audit.

## Where we are now
Updated whenever a criterion is closed. Progress = closed criteria / all criteria.

| Stage | Name | Criteria closed | Status |
|---|---|---|---|
| 0 | Foundation | 4 / 4 | done |
| 1 | Autonomous run of a single spec (forge) | 5 / 5 | done; outcome is a simplified core (A22) |
| 2 | Gates: the script decides | 5 / 9 | in progress |
| 3 | Spec preparation | 2 / 5 | in progress |
| 4 | Work-unit queue | 1 / 6 | in progress |
| 5 | Results audit | 1 / 6 | in progress |
| 6 | Attended mode | 3 / 3 | done (by tests) |
| 7 | Pilots on two types of systems | 0 / 5 | not started |
| 8 | Release 1.0 | 1 / 5 | in progress |
| | **Total** | **22 / 48 (46%)** | |

Decision point after stage 1: if forge is no better than plain in quality and no cheaper at equal quality, the core is simplified to "plain + ambiguity audit + frozen tests", and stages 2–8 are built around it unchanged.

## Stage 0. Foundation — done
| # | Criterion | Verification |
|---|---|---|
| 0.1 | Survey of techniques with sources | `docs/research/*.md` |
| 0.2 | Design with options and a choice | `docs/design-forge.md`, `docs/design-system.md` |
| 0.3 | Survey of ready-made solutions, "build / adopt" conclusion | `docs/research/ready-made-plugins.md` |
| 0.4 | Separate plugin repository | github.com/KirillMaster/powerpuff |

## Stage 1. Autonomous run of a single spec (forge)
Goal: one spec → working code without a human; "done" is printed by the script.
| # | Criterion | Verification | Status |
|---|---|---|---|
| 1.1 | A real spec is run end to end without intervention, result re-verified independently | `FORGE: done`, own run of tests and linter | closed: v2, 2026-11-17, `FORGE: done`, $8.09, 220 tests and linter re-verified; one stop was caused by a driver bug (fixed), a repeat without intervention is on the bench |
| 1.2 | Resume after an interruption and after a subscription limit, cumulative budget | test: kill the process at every step → rerun reaches the end; fake "limit" response → wait and continue | closed 2026-11-17: `tests/test_forge_run.py`, 41 tests with a fake `claude` (interruption at every step, subscription limit, budgets, all transitions); 3 driver bugs found and fixed |
| 1.3 | Driver unit tests with a fake `claude` | `pytest tests/` green, all state transitions covered | closed 2026-11-17 by the same test suite as 1.2 (`tests/test_forge_run.py`, see row 1.2) |
| 1.4 | Bench report: plain, lite, bob, forge, forge-opus on a shared task set; quality, cost by model, fit within subscription limits | `protocol_verdict.py` over two runs, unambiguous verdict | closed 2026-11-18: forge equals plain in quality at ×3–4 the cost (×8 on a chain), the role-based pipeline is worse (FINDINGS A22) |
| 1.5 | Core decision recorded: forge as is / simplified core; default mode in one line | entry in `docs/FINDINGS.md` and README | closed 2026-11-18: simplified core; the default-mode line is in the README |

## Stage 2. Gates: the script decides
Goal: no model claim affects the verdict. Design — `docs/design-gates.md`.
| # | Criterion | Verification |
|---|---|---|
| 2.1 | `powerpuff.yaml` profile in the project repository; the driver runs the gates; an unapproved profile is flagged in the report | forge does not read model-chosen gates without a flag |
| 2.2 | Missing tool or report — gate failed | test with a missing tool |
| | ↳ 2.1 and 2.2 closed 2026-11-17 | `powerpuff.yaml` profile (iteration / unit / final levels), draft and a "gates not approved" flag when there is no profile, missing tool = failure, bypass protection in the "done" check, the driver does not trust `.forge/gates.json` and the profile if they changed during the run; `tests/test_forge_profile.py`, whole suite — 109 tests |
| 2.3 | `gate_guard`: skipped tests, suppressed warnings, edits to protected files, reduced test count | tests for ≥8 planted bypasses, all caught; 0 false positives on 3 real diffs |
| | ↳ 2.3 closed 2026-11-17 | `scripts/gate_guard.py`, 43 tests green; languages: C#, MSBuild, JS/TS, Python, Rust, Go, Java/Kotlin, custom ones via the profile. No false positives on three real diffs: the clean forge v2 diff — 0 violations; the forge v1 diff — 2 genuine `@ts-expect-error` in tests; a large diff with third-party subprojects — genuine `# noqa` and new configs in subfolders. Wiring into the driver is criterion 2.1 |
| 2.4 | Single `quality-report.json`; core with no knowledge of stacks; profile presets for .NET (via `qg`), TypeScript, Python, Rust, Go | one real repository for at least three different stacks, one of them without a preset (profile written by hand): report obtained by the driver |
| | ↳ 2.4 closed 2026-11-17 | `presets/*.yaml` presets (.NET, TypeScript, Python, Rust, Go), `scripts/profile_init.py` (draft profile from markers, `approved: false`), `scripts/quality_report.py` (single `quality-report.json` with the time of each gate). Verified on three real repositories: .NET, Python and Kotlin/Gradle without a preset (profile by hand) — table in `docs/profile.md`, finding A20. Not verified: Rust and Go presets (no tools on the machine), the `final` level |
| 2.5 | Unit gates: diff coverage, complexity and duplicates on changed files, "acceptance test fails on old code" | tests for planted violations |
| | ↳ 2.5 closed 2026-11-17 | `scripts/diff_gates.py`: changed-line coverage (cobertura, lcov, Go, coverage.py, JaCoCo), new duplicates, complexity of touched functions (lizard, eslint, radon, gocyclo, SARIF), diff size; no report = exit code 3; 62 tests for planted violations; "acceptance tests fail before implementation" — `tests/test_forge_red.py`. The base commit is substituted by the driver (`{base}`) |
| 2.6 | Measurement: time of each gate and what it caught on the bench | table in `docs/FINDINGS.md`; gates that caught no defects are made optional |
| 2.7 | Test-forgery gate (script, no model): test without assertions, tautological assertion, mock of the code under test, test does not fail when the implementation is reverted, weakened assertions in changed tests | tests for planted forgeries; on the bench — how many it caught and false positives |
| 2.8 | Diff mutants and CRAP (complexity × uncovered share) of changed functions — `final` level, optional | bench measurement: time and what it caught; stays only if it catches what the gates missed |
| 2.9 | "Done" only with proof: the report contains the commit SHA, gate commands with exit codes and output tails, hashes of frozen tests | `quality-report.json` matches the schema; missing fields — `FORGE: failed` |

## Stage 3. Spec preparation
Goal: only a spec that requires no guessing goes into autonomous mode.
| # | Criterion | Verification |
|---|---|---|
| 3.1 | `spec_lint`: criterion without a test, story without non-functional requirements, nonexistent path, task list too long, deferred item without an owner | tests for 5 planted defects |
| | ↳ 3.1 closed 2026-11-17 | `scripts/spec_lint.py`, 30 tests for planted defects (criterion without a test, story without non-functional requirements, nonexistent path, long task list, deferred item without an owner, unresolved clarification, dangling reference, requirement without a task); does not crash on two real specs, both are rejected: one has a task list of 1208 lines, the other has a state-changing story without non-functional criteria |
| 3.2 | Ambiguity audit produces questions in the "blocking / non-blocking / informational" format with a default decision | run on 2 real specs; a human resolves the blocking ones in a single pass |
| | ↳ 3.2 partial | done: the audit writes questions as `Q<n> [blocking|non-blocking|info]` with a default decision and the cost of a mistake, the driver parses them into `.forge/questions.json`; without an answer to a blocking question the run does not guess (or `--assume-defaults`), answers go to the writers as mandatory decisions (`tests/test_forge_hardening.py`). Open: run on 2 real specs |
| 3.3 | Slicing into units `{id, dependencies, requirements, risk level, files}` with a target diff of up to ~200 lines | `queue.yaml` for a real spec; actual diff within the limit for ≥80% of units |
| 3.4 | "How to build and test" is verified by running it before start | the driver refuses to start on a red base |
| | ↳ 3.4 closed 2026-11-17 | all gates run on the untouched base before the first model session; red base → refusal with output tails (`--allow-red-base` is a deliberate bypass), missing tool → always refusal; `tests/test_forge_hardening.py` |
| 3.5 | The prepare command chains the steps into one pass | from idea to `queue.yaml` in one command, with questions along the way |
| | ↳ 3.3, 3.5 partial | `scripts/prepare_run.py` + `/powerpuff:prepare`: spec linter → green base → questions (blocking ones — stop with exit code 4 until answered) → slicing into units → `queue.yaml`; the script validates the slicing (cycles, file overlap, coverage of all requirements, size estimate ≤ limit), up to two model fixes; 23 tests with a fake `claude`. Open: a real spec and measurement of actual unit diffs |

## Stage 4. Work-unit queue
Goal: an autonomous run of any length that does not stop because of a single failure.
| # | Criterion | Verification |
|---|---|---|
| 4.1 | The driver walks `queue.yaml`: own branch and worktree per unit, PR per unit | run of ≥5 units, 5 PRs |
| 4.2 | A failed or blocked unit does not stop the run | run with one deliberately infeasible unit: the other independent ones are done |
| | ↳ 4.2 partial | mechanism is ready and covered by tests with a fake `claude` (`tests/test_queue_run.py`): a unit failure blocks only its dependents, independent ones are merged; a unit with a blocking question gets the `needs-answer` status. Open: a real run (together with 4.1) |
| 4.3 | Subscription limit: wait for the reset and continue; total run budget, a third reserved for fixes | fake limit in the middle of a run |
| | ↳ 4.3 closed 2026-11-17 | `scripts/queue_run.py`: fake limit → wait until the reset or exit with code 3 and continue by rerunning; total run budget, a third reserved for fixes, shortage → `skipped-budget`; tests in `tests/test_queue_run.py` |
| 4.4 | Auto-merge of low-risk units with green gates into the integration branch; high risk waits for the audit | check on the run from 4.1 |
| | ↳ 4.1, 4.4, 4.5 partial | `queue_run.py`: branch and worktree per unit, merge of low-risk units into the integration branch without touching the working copy, high risk waits for the audit, final-loop commands on the integration branch — covered by tests with a fake `claude`. Open: PR per unit, a real run of ≥5 units, the fix unit from a final-loop failure (done by `audit_apply.py`) |
| 4.5 | Heavy loop at the end of the run on the integration branch; failure → card and a fix unit | profile with a heavy loop on one repository |
| 4.6 | One independent acceptance per run, not per unit; cost comparison with per-unit acceptance | measurement on the run from 4.1 |

## Stage 5. Results audit
Goal: a human spends 10–20 minutes and their answers become the input of the next run.
| # | Criterion | Verification |
|---|---|---|
| 5.1 | `cards.html`: default decisions, open questions, high-risk units with a diff, failures with evidence, summary with cost | generated from a real run |
| | ↳ 5.1 partial | `scripts/audit_cards.py`: one self-contained `cards.html` (questions, high-risk units with a diff, failures with evidence, blocked ones, final loop, summary), hostile text neutralized; 21 tests. Open: generation from a real run, check in a browser |
| 5.2 | Answers are exported to `answers.yaml` | file passes the schema |
| | ↳ 5.2 closed 2026-11-17 | `answers.yaml` (format — `docs/audit-format.md`); `scripts/audit_apply.py` validates it against the run: unknown card or invalid decision → exit code 3 |
| 5.3 | Answers produce spec edits and new units | the next run starts from them without manual edits |
| | ↳ 5.3 partial | `audit_apply.py`: answers are written to `questions.json`, accepted units are merged, rework/retries/fixes → `next-queue.yaml`, which `queue_run.py --dry-run` accepts. Open: a real next run |
| 5.4 | Auditing a 5-unit run takes ≤20 minutes | measurement in the pilot |
| 5.5 | Critic on a different model — `--critic` option at the `final` level: a finding is accepted only with proof "file:line + input → wrong result", and the script confirms it with a failing test; unconfirmed ones are discarded | bench measurement: share of confirmed findings and cost; into the default mode only if plain + critic beats plain |
| 5.6 | Exam for harness changes: a new rule, skill or prompt is accepted only if the small bench set (plain, 5 tasks, k=2) is no worse than before | comparison script and an entry in `docs/FINDINGS.md`; worse — rollback |

## Stage 6. Attended mode
| # | Criterion | Verification |
|---|---|---|
| 6.1 | The same queue with a stop after each unit and immediate questions | `--attended` flag |
| | ↳ 6.1 closed 2026-11-17 | `queue_run.py --attended`: pause after each unit (code 4), rerunning continues; a unit with a blocking question — `needs-answer`; tests in `tests/test_queue_run.py` |
| 6.2 | The Stop hook does not let a turn finish while iteration gates are red | hook test |
| 6.3 | The PreToolUse hook blocks edits to protected files | hook test |
| | ↳ 6.2, 6.3 closed 2026-11-17 | `hooks/hooks.json`, `scripts/hook_stop.py`, `scripts/hook_protect.py`; enabled in the profile (`hooks:`), off by default; loop protection; 22 tests (`tests/test_hooks.py`). Not verified in a live Claude Code session — item D in `docs/FINDINGS.md` |

## Stage 7. Pilots on two types of systems
Goal: prove generality — a stateful server and a service with an HTTP API.
| # | Criterion | Verification |
|---|---|---|
| 7.1 | One stage of real work on each type of system is completed through the full cycle | two cycles: preparation → run → audit |
| 7.2 | Rework share is lower than the role-based pipeline had (20–26%) | commit history |
| 7.3 | Defects found after "green" are counted and analyzed: which gate catches each one | table in `docs/FINDINGS.md` |
| 7.4 | Cost per unit and number of units per run within the subscription | run report |
| 7.5 | Two runs in a row without manual code edits between them | pilot log |

## Stage 8. Release 1.0
| # | Criterion | Verification |
|---|---|---|
| 8.1 | Plugin installation and first run per the README in ≤15 minutes on a clean repository | check on a fresh clone | verified 2026-11-19 locally: `claude plugin marketplace add <clone>` + `install` in a clean `CLAUDE_CONFIG_DIR`, then `/powerpuff:forge` on a fresh TS clone without `node_modules` and a profile → `FORGE: done` in ~2 min; close after the push by checking `marketplace add KirillMaster/powerpuff` |
| 8.2 | Migration guide from bob-pipeline | verified on one project |
| 8.3 | The plugin's own CI: tests, linter | green on `main` |
| | ↳ 8.3 closed 2026-11-17 | `.github/workflows/ci.yml`: Linux and Windows, Python 3.11 and 3.12, pytest + ruff; green on `main` (commit 34eb106, 399 tests). The first run caught syntax that is invalid in 3.11 |
| 8.4 | All "to verify" items from `docs/FINDINGS.md` are closed or dropped | section D is empty |
| 8.5 | Everything that showed no value in measurements is removed | reconciliation with 2.6 and 7.3 |

## Order and dependencies
`1 → 2 → 3 → 4 → 5 → 7 → 8`; stage 6 can be done at any time after 2. Stages 2 and 3 do not depend on each other and can run in parallel.
