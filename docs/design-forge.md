# forge — spec → finished program, turnkey (design, 2026-11-17)

Inputs: `docs/research/harness-survey.md` (techniques and evidence), the 2026-11-16 pilot (`bench/results/2026-11-16-pilot`),
model calibration (`2026-11-16-calib-{sonnet,haiku,tickets-opus}`), the log `bench/results/DECISIONS-representative.md`.

## Facts the choice rests on
| Fact | Source | Consequence |
|---|---|---|
| plain Opus: behaviour 0.94–1.0, ~$1, 4–15 min | pilot | the baseline is strong; the harness must not get in its way |
| lite = plain in quality, ×2–3 cost | pilot | the ritual "checklist + verify + gate loop" does not pay off by itself |
| bob: $9–20, timeout, 0.70–0.78 | pilot | per-slice roles and fresh context — removed |
| Sonnet plain on the same tickets (old revision) = 1.0 for $0.26–0.49, 1–3 min; Haiku — timeouts | calib-sonnet/haiku | implementation can go to Sonnet; Haiku only for mechanics |
| plain and lite misses coincide and lie in ambiguous places of the ticket (deskhub: pause/resume, skip cap after renew, validation order) | pilot failing_hidden | quality is raised only by working on the spec before code, not by extra checkers |
| Anthropic: a planner is needed, the evaluator is a single pass at the end; remove components one at a time | survey §1 | one final verifier, no per-slice checks |
| Multi-agent ×4–15 tokens, the writer must be single | survey §1 (Cognition, Anthropic) | one executor writes code at a time; several agents only for reading and checking |

## Options
| | A. plain+ | B. forge (chosen) | C. Ralph-loop |
|---|---|---|---|
| Essence | One Opus does everything, plus a spec audit and a final verifier | Opus: spec audit → plan → dispatcher → acceptance. Sonnet: acceptance tests, implementation. Haiku: gate runs, mechanical edits | An outer loop: a fresh `claude -p` (Sonnet) per task from `tasks.yaml`, a progress file; Opus only at the start and the end |
| Opus tokens | 100% | ~20–35% (audit, reviews, acceptance) | ~10% |
| Risk | violates the constraint "Opus only orchestrates" | context loss on the Opus→Sonnet handoff | every session re-reads the repo; context cost ×N; weak architectural consistency |
| Evidence | plain is already ≈1.0 | Aider architect/editor +2–5 pts; Sonnet ≈ Opus on calibration | anecdotes only (survey) |

**Choice: B.** It is the only option that meets all the goal's constraints (Opus only orchestrates and checks, cheap models execute), and every part of it has at least some data. From C we take the outer driver with a state file, to continue after a rate limit. A is the fallback conclusion "plain plus X" if B loses.

## Answers to the 6 mandatory questions
1. **Ambiguities before code.** The `audit` step (Opus, a single read-only pass) reads the spec and the relevant places in the code and writes `.forge/decisions.md`. In it, every place that allows two readings, the chosen reading and the basis. The selection rule, in order: existing code, tests and conventions; the literal text of the spec; consistency with other items; the stricter or safer interpretation. If both readings are compatible, we implement both (for example, accept both input forms). The autonomous run never stops. There is a separate mode `/forge --preflight` for running with a human present: it does only the audit and asks the blocking questions as a single list; the answers are written to `decisions.md` as `D<n> (human)`. The autonomous run takes this file as ready.
2. **Executable spec.** The `acceptance` step: a separate Sonnet subagent writes acceptance tests through the public interface (HTTP/CLI/API). Each test is marked `R<n>` and references a spec item or `D<n>`. The subagent does not see the implementation plan. Opus checks coverage: every `R` is covered by at least one test. After that the tests are frozen: `sha256` is written to `.forge/frozen.sha256`, and the `frozen` gate checks the sums after every task. The executor does not touch the tests. If a test contradicts the spec, the executor writes a `dispute` to state, and Opus resolves the dispute at acceptance; only it can unfreeze a test, with an entry in the report. There is no hidden set in production mode (the bench plays its role). Protection comes from the freeze and a separate test author.
3. **Model split.** Opus (main thread) does the audit, the plan (groups `T<n>` from `tasks.yaml` or from the spec), briefs, failure analysis and the decision "retry, escalate or accept". Sonnet: acceptance tests; implementation of each group (one executor at a time, a fresh subagent per group with a brief: goal, `R` identifiers, files from the digest, conventions, gate commands); fixes after acceptance. Haiku: the repo digest (`.forge/digest.md`, path:line, once), gate runs and format/lint fixes. Opus does not re-read the repo: it works from the digest, the executors' short JSON reports and `git diff --stat`. Escalation: if Sonnet fails the same group twice, Opus makes a third attempt itself, in its own context, with the full failure log.
4. **Quality loop.** After each group the mechanical gates (format, lint, architecture, tests, frozen). The executor runs them itself, and for gates from `bob-pipeline.yaml` — `scripts/ci_gates.py`. Mutation testing only if the tool is already in the project config (`tools[]` of category mutation), deterministic, on changed files, with no LLM role: there is no evidence of an effect on hidden tests (survey). There is a single independent acceptance, at the end: an Opus subagent with fresh context and read-only access cross-checks the spec, `decisions.md`, the diff and the test results and looks only for correctness gaps (skips style). Then at most 2 rounds of fixes by Sonnet. There are no per-task checks: Anthropic removed them, and in the pilot they cost ×10.
5. **Long run.** State is kept in `.forge/state.json`: phase, tasks with statuses, attempts, disputes, budget. `/forge` is always idempotent: it reads state and continues from the first unfinished task. Each group ends with a commit. The driver `scripts/forge_run.py` runs `claude -p "/bob-pipeline:forge …"` in a loop: on a rate limit it sleeps until the reset time (parses the message; otherwise 15 min), on a break it does `--resume` or a new session with the same state; it stops on the marker `FORGE: done|failed`, on `--max-hours` or `--max-usd` (sum of `total_cost_usd`). Progress is visible in `.forge/progress.md`, one line per event.
6. **Result.** `.forge/REPORT.md` no longer than one screen: status; a table `R → test → pass`; gates (last run); decisions `D<n>` where the spec was silent; disputes and unfrozen tests; what is in question; cost by model. **Turnkey done** = all `R` covered by green tests, frozen hashes intact, all gates green, acceptance has no open gaps → `FORGE: done`. Otherwise `FORGE: failed` with a list of what is not closed.

## What we deliberately do not do (and why)
- No architect, hardener and QA roles per slice: the pilot showed ×10 cost with no gain.
- No self-consistency (N samples): all samples share the same misreading, and our misses are exactly that kind.
- No worktree per slice: there is one executor and it works sequentially.

## Verification
The `forge` variant in `bench/bench.yaml`, compared per `bench/PROTOCOL-representative.md`. Hypothesis H-forge: forge ≥ plain in quality (≥ +0.03 or equal) with Opus share ≤ 40% of cost. If quality is equal and cost is higher — an iteration (no more than 3), then the conclusion "plain plus X".

## Revision after additional research (2026-11-17)
Sources: `docs/research/harness-survey.md` §5, `docs/research/multi-agent-evidence.md`, `docs/research/hackathon-winner-ecc.md`.
- **Confirmed:** a single writer; parallel only read-only; tests from the spec before code and a freeze (tests written separately from code catch 25% of defects, after code — 14%); one fresh acceptance at the end; state on disk; an iteration limit.
- **Changed:** the `done` verdict is issued by the script `scripts/forge_check.py`, not by the model (as in Tekton: SHIP is decided by orchestrate.mjs against a rubric). The script checks that R is covered by tests, acceptance tests are green, frozen hashes are intact and gates are green. The LLM acceptance adds gaps but does not set green itself.
- **In question:** the split "Opus leads, Sonnet writes" is not proven by the literature (ManagerWorker 62 vs 60%, the Aider pair 78.2 vs 88.0% for a single model). We check by an ablation on the bench: `forge` (writer Sonnet) against `forge-opus` (writer Opus, the rest the same). The protocol rule decides.

## Iteration 2: forge v2 — script orchestrator (2026-11-17)
The trigger is a measurement of v1 on a real spec (a web project, a spec of 16 requirements, 16 requirements, diff +727/−60):
| Metric | forge v1 |
|---|---|
| Outcome | `FORGE: done`, 59 tests green, hashes intact |
| Cost (nominal) | $11.42: Opus 54%, Sonnet 46% |
| Where from | Opus orchestrator: 70 turns of ~80K tokens of context (5.7M tokens of cache reads); Sonnet workers: 94 turns, 6.3M |
| Conclusion | the cost is built into the architecture: the orchestrator model pays for every turn with its entire context, and every new worker re-reads the repository |

Changes:
| Was (v1) | Now (v2) | Basis |
|---|---|---|
| The orchestrator is an Opus session (`commands/forge.md`) | The orchestrator is the script `scripts/forge_run.py`: state, step order, hashes, gates, budget, limit waiting | deterministic work does not need a model; in Tekton the decision is also made by a script |
| Haiku digest + audit + 3–8 workers by group | 4 session types: audit (Opus), acceptance tests (writer), build (writer, one context, continues on a red check), acceptance (Opus, fresh context) | one writer with continuous context; the repository is read once |
| The model declares "done" after `forge_check` | `forge_check` calls the script; the script prints the line `FORGE: done` | the model's word does not count |
| A modified frozen test is fixed by the orchestrator | the script restores the file from git before running the gates | deterministic |
| Opus takes over the work itself | at most 3 writer rounds, then one session of a strong model, then `failed` with a report | bounded cost of failure |

Iteration criterion: the same quality on the same spec at a noticeably lower cost than v1; on the bench — no worse than plain in quality. If v2 does not beat plain on cost either, the honest conclusion by the prompt's rule: "the best harness = plain plus ambiguity audit and frozen acceptance tests".
