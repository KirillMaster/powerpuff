# powerpuff efficiency (`plain` engine) — report 2026-11-19

Question: does powerpuff (`forge_run.py --engine plain`, the default) give better results than plain Claude Code (a single Opus session with no harness), and what does it cost.

## Summary
| What | plain (Claude Code, no harness) | powerpuff `plain` |
|---|---|---|
| Behavior quality (hidden tests), 6 tasks with a result in both | 0.968 | **0.996** |
| Tasks where behavior is better / equal / worse than plain | — | 3 / 3 / 0 |
| Price per task, $ | 0.67 | 1.46 (×2.2) |
| Time per task, min | 8 | 19 (×2.3) |
| Engine failures | — | 2 of 8 (TS: no dependencies in the repo; fixed, not re-measured) |
| Fresh repository without a profile (Python, 4 requirements) | — | `FORGE: done` in 88 s, $0.50, 7 gates from the preset, tests red before the code and green after |
| Fresh TypeScript clone without `node_modules` and without a profile (4 requirements), after fixes | — | `FORGE: done` in 121 s, $0.56: `setup` installed dependencies, all 6 preset gates active (typecheck, build, tests, …), requirement coverage 4/4 |

Conclusion: on tasks that reached a result, powerpuff never lost to plain and won on three of six; on those, plain dropped requirements, and the frozen acceptance tests and gates caught the losses. The price is roughly double the money and time. At n=1 per task the quality difference is within noise (+0.028, the confidence interval crosses zero); the price difference is not.

**When powerpuff, when plain**
| Situation | Mode |
|---|---|
| Unattended work (autonomous run, spec queue), a verifiable "done" is needed | powerpuff: frozen tests, script-run gates, a report with evidence |
| A many-requirement spec where a missed requirement is costly | powerpuff: tests for every `R<n>` before the code |
| A small edit with a human nearby | plain: half the price, same quality |

## How it was measured
| Parameter | Value |
|---|---|
| Set | 8 tasks from real work repositories (Python ×4, TypeScript ×3, C# ×1) with hidden acceptance tests; 3 more C# tasks and a multi-stage project did not finish (the run was stopped by a lack of memory on the host) |
| Repeats | k=1 per task (plain — the same tasks from the 2026-11-17 run) |
| Isolation | Docker, identical images and limits; scoring — hidden tests, regression, gates, judge (`bench/scripts/protocol_verdict.py`) |
| Model | Opus in both variants |
| Price | from the CLI's `total_cost_usd`; for `--resume` sessions the increment is counted (see "Bugs found by the bench", item 3) |

## By task
| Task | Behavior plain → powerpuff | Overall plain → powerpuff | $ plain → powerpuff | min plain → powerpuff |
|---|---|---|---|---|
| py-1 | 1.000 → 1.000 | 0.963 → 0.955 | 0.28 → 0.69 | 3 → 8 |
| py-2 | 0.909 → **1.000** | 0.903 → 0.974 | 0.65 → 1.29 | 5 → 12 |
| py-3 | 1.000 → 1.000 | 0.959 → 0.970 | 0.45 → 1.07 | 5 → 12 |
| py-4 | 1.000 → 1.000 | 0.963 → 0.959 | 0.76 → 1.51 | 5 → 13 |
| ts-3 | 0.952 → **1.000** | 0.850 → 0.891 | 0.85 → 1.88 | 21 → 35 |
| cs-1 | 0.947 → **0.978** | 0.851 → 0.872 | 1.03 → 2.33 | 10 → 34 |
| ts-1, ts-2 | 1.000 → 0 (engine failure) | — | 0.6 → 0 | — |

## Bugs found by the bench (all fixed, covered by tests)
| # | Bug | Consequence | Fix |
|---|---|---|---|
| 1 | The duplicate gate flagged a clone inside the frozen tests | 4 empty fix rounds, ×4 on price | clones inside frozen tests are not counted; copy-paste from them into code is |
| 2 | A fix round with no changes repeated up to the limit | extra sessions | a round with no changes in the tree ends the loop |
| 3 | `claude -p --resume` returns the session's cumulative price, and the driver summed it | the price was overstated; for forge in the bench ×1.6–2.2 | the increment is counted; the forge recalculation is in the bench report |
| 4 | Draft profile: a gate without a tool failed the run before the first session | failure of the TS tasks (ts-1, ts-2) | a gate without a tool in a draft is excluded and named in the report |
| 5 | No dependencies installed (fresh clone) → test gates red on the base and excluded | on these tasks powerpuff checked only static gates | the `setup` key in the profile, presets install dependencies before the base check (after the run, not re-measured) |
| 6 | Spec requirements `R<n>` were not matched against tests | report "requirements: 0" | an id without a test → one "finish the tests" turn, the remainder is `uncovered` in the report |

## Report limitations
| Limitation | Impact |
|---|---|
| k=1, 6 task pairs | quality difference within noise; a k=2 run on all 12 tasks is needed |
| In the bench images the test gates were excluded for the Python and TS tasks (dependencies not installed, item 5) | powerpuff's advantage on these tasks comes from frozen tests and questions, not gates; more is expected with `setup` |
| Items 4–6 were fixed after the run | ts-1, ts-2 not re-measured on the bench; `setup` verified by a real run on a fresh TS clone (the "Summary" table) |
| The multi-stage project and 3 C# tasks did not finish | no data on follow-up changes on top of its own code |

## How to work with Claude efficiently (from all measurements)
| Rule | Why (measurement) |
|---|---|
| One Opus session per spec slice, no role-based pipeline | the role-based pipeline is worse than plain (Δ −0.28), multi-session forge — the same quality at ×3–4 |
| Before the code — questions on ambiguities and acceptance tests the model cannot edit | powerpuff caught missed requirements on 3 of 6 tasks |
| "Done" is decided by command exit codes, not the model's words | all 6 powerpuff "done" were confirmed by hidden tests: 5 fully, 1 at 88 of 90 |
| The gate profile is approved by a human, dependencies are installed before the base check | draft gates without dependencies silently weaken (item 5) |
| Count price by the increment, not by `total_cost_usd` with `--resume` | otherwise orchestrators look more expensive than they are (item 3) |

Data: `bench/results/2026-11-19-powerpuff/` and `REPORT-representative.md` in the bob-pipeline repository (bench), the decision log — `DECISIONS-representative.md`.
