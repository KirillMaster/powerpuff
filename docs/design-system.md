# powerpuff — spec preparation, autonomous run, audit (system design, 2026-11-17)

Goal: a general solution for building quality software systems of any complexity — fast, cheap, stable. The human invests in the spec and the audit; the code is written while they rest. An autonomous run can start at any time, with no tie to the hour of day; there is also a mode with the human present. Nothing project-specific lives in the core.

## Shared core and project profile
| Layer | What it holds | Where it lives |
|---|---|---|
| Core (one for all projects) | spec linter, ambiguity audit, unit queue, frozen acceptance tests, single writer, `forge_check`, cards, budget and resume | plugin |
| Project profile | gate commands (format, linter, architecture, tests), heavy contour, non-functional criteria by system type, auto-merge rule | `.forge/gates.json` and `powerpuff.yaml` in the repository; the audit writes the first draft from the manifests |

The heavy contour is set by the system type, not the project:
| System type | Heavy contour |
|---|---|
| Service with an HTTP API | OpenAPI contract tests, integration tests with a real DB and broker in containers, load smoke run, migration check |
| Stateful server with its own protocol (e.g. a game server) | bot over a real network, reference packet bytes, soak, fuzz, invariants (no dupes, balance) |
| Web UI | end-to-end browser tests, screenshots, accessibility |
| Library or CLI | property-based tests, public API check for breaking changes |

The core handles system complexity by slicing: a big system is a long queue of small units with dependencies, not one big run.
Sources: `docs/research/{harness-survey,multi-agent-evidence,hackathon-winner-ecc,trends-2026-async-agents}.md`, the bob-bench pilot, `docs/design-forge.md` (the autonomous executor core).

## Observations from a real project built with a role-based pipeline
| Observation | Consequence for the system |
|---|---|
| About a quarter of the coder's work is rework; about half of the fixes are gate mechanics (duplicates, format) | cheap autofix for mechanics, not roles on every slice |
| Some bugs were found only by a live client after green QA | a live path through the real interface is a mandatory gate |
| Heavy tests are off by default, there is no long run, no list of flaky tests | heavy contour and quarantine for flaky tests |
| There are three times more acceptance criteria than tests; specs stay in draft status forever; paths in tasks are stale | spec linter and a machine-readable status |
| In autonomous mode the orchestrator made the decisions, and there is no list of open questions | questions at spec preparation, autonomous-run decisions at the card audit |
| The remaining stages are mostly independent | a queue of independent units for the autonomous run |

## Three modes
### 1. Spec preparation (human present, 30–90 min)
1. `grilling` → `yamlkit-specify/plan/tasks` (as now).
2. **`spec_lint.py`** (script, deterministic). Fails if:
   - an AS has no test identifier (TS < AS);
   - a state-changing story has no non-functional criteria (tick budget, memory, reconnect, kill -9, no dupes);
   - a path from tasks does not exist in the tree;
   - tasks is longer than ~600 lines;
   - a deferred item has no owner.
3. **Ambiguity audit** (`/forge --preflight`, Opus). Produces `R`/`D` lines and question cards. The human closes the blocking ones right away; the rest get a default decision and go to the audit. Rationale: models cannot tell an incomplete task from a complete one (Ambig-SWE), and the only remedy with an effect is to ask before starting.
4. **Slicing into units** `{id, deps, R[], tier, owned_files}`. Target: diff ≤ ~200 lines per unit (changes up to 50 lines merge in 76–80% of cases, 101–500 in 64%; the number of review rounds grows with size). Units that share files run sequentially.
5. The repository map (`digest.md`: how to build and test) is verified by running it. In dotnet/runtime, build instructions raised agent success from 38% to 69%.
Result: `queue.yaml` — a queue of units with statuses. It is also the stage status, machine-readable.

### 2. Autonomous mode — execution (no human)
The `forge_run.py` driver walks `queue.yaml`. For each unit:
1. Acceptance tests from `R` are written in a context that has not seen the implementation, and are frozen (25% of defects caught versus 14%).
2. One writer, fresh context; gates; Haiku fixes the mechanics.
3. `forge_check.py` decides whether it is done. The model's word does not count (Tekton's approach).
4. A branch and a PR per unit. `tier: low` and a green check → auto-merge into the integration branch. `tier: high` → waits for cards.
5. A blocked or twice-failed unit gets a card, and the driver takes the next independent one. The run does not stop.
6. About a third of the budget is reserved for fixes (Factory: 34%).
At the end of the autonomous run, the **heavy contour** from the project profile runs on the integration branch (see the table by system type). A failure → a card and a fix unit in the queue. One independent acceptance (fresh Opus) per run, not per unit.
Budget: its own cumulative limit (the built-in one does not count resumed sessions), waiting for the rate limit to reset, resume from `queue.yaml`.

### 3. Audit (10–20 min)
`cards.html` — cards modeled on the `hld-decision-cards` skill, one for each of:
- a decision `D` ("spec was silent → X chosen because …"; accept or change);
- an open question;
- a `tier: high` unit (diff stat, tests, merge button);
- a failure (what failed, evidence);
- the run summary (done / queued / cost by model).
Answers are exported to `answers.yaml`. From them come spec edits and new units; the next run starts from them. Only what the human accepted at the audit lands in `main`; for `tier: low` this can be replaced by an auto-accept rule.

**Attended mode:** the same queue, but it stops after each unit and asks questions right away (`--attended`).

## Models
| Work | Model | Rationale |
|---|---|---|
| Spec audit, slicing, failure analysis, final acceptance | Opus | this is where our misses are; planning and structure give ~+11 pts (ManagerWorker) |
| Acceptance tests and unit code | Sonnet by default; an ablation of `forge` versus `forge-opus` will decide | the literature shows a tie on quality, a win on price |
| Digest, gate runs, mechanics autofix | Haiku | about half of the fixes in the analyzed project are mechanics |

## What exists and what to build
| Component | State |
|---|---|
| Autonomous-mode core for a single spec (`/forge`, `forge_run.py`, `forge_check.py`) | prototype; a check on a real spec and a bench are in progress |
| `--preflight` | present in `/forge`, without cards |
| `spec_lint.py` | to build (script) |
| `queue.yaml` and queue traversal by the driver, branch/PR per unit | to build |
| `cards.html` and `answers.yaml` | to build (reuse hld-decision-cards) |
| `powerpuff.yaml` (project profile: system type, heavy contour, auto-merge rule) | to build; templates for the four system types |
| A specific project's heavy contour | the project's own tasks, queued first |

## Proof
1. bob-bench (in progress): plain / lite / forge / forge-opus per the protocol — quality and cost of the core.
2. A pilot on two different system types to check generality: a stateful server and a service with an HTTP API. Metrics against the project's history:
   - rework share (was ~20–26%);
   - how many bugs a live client finds after "green";
   - audit minutes;
   - cost by model;
   - units per run.
3. Rule: a component stays only if its contribution is visible in these metrics. Remove them one at a time (Anthropic's rule).

## Honest limits
- Without a human, ambiguity is not resolved, only recorded; hence cards, not a promise of "zero questions". A blocking question without an answer stops the run (`blocking questions: Q1,Q4`, exit 1; after answering in `.forge/questions.json`, a re-run continues from the same place); `--assume-defaults` takes the auditor's default, and such questions go into the report as a separate section.
- Having a spec does not by itself reduce defects (ESEM 2026, 88k PRs). What works is verifiable criteria and frozen tests.
- There is no data on soak and replay in agent execution; property-based tests give 56% valid findings (n=50). Stability is verified only by a pilot.
