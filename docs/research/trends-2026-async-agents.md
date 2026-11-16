# Trends 2026: async / overnight multi-agent dev + morning audit (2026-11-17)

Scope: NEW vs harness-survey / multi-agent-evidence / hackathon-winner-ecc (not repeated: Anthropic harness posts, Cognition single-writer, Google scaling, MAST, Aider, Tekton/ECC, tests-first, mutation, headless flags).
Tags: V = primary page read this session, S = search snippet / secondary, U = unverified.

## 1. Async coding-agent products

| Product | Unit of work / review batching / blocked handling | Outcome data | Tag |
|---|---|---|---|
| Factory Missions | Orchestrator interviews the user to unambiguous requirements -> validation contract (finite testable assertions, written BEFORE features) -> features grouped in milestones -> fresh worker per feature (tests first) -> validators (code + black-box) at milestone end; validators report, do not fix -> fix features -> re-validate. Blocked = mission halts, control returns to user. No morning-review UI or notification documented. https://factory.com/news/missions-architecture | Slack-clone case: 16.5h, 6 milestones, 185 runs, 778.5M tokens (744.9M cache read), 38.8k LoC (52.5% tests), validation = 37% of time; milestones passing 0/6 -> 1/6 -> 2/6 -> 6/6 over 4 rounds; 21 fix features = 34% of impl; 81 issues (65 blocking). Fleet: 14% of missions >24h, longest 16 days (S) | V |
| Devin | Session = one PR-sized task (4-8h junior work); human-written playbooks (e.g. unit-test playbook over hundreds of repos); fleet migrations. Cannot take an ambiguous project end-to-end; mid-task instruction changes degrade results. Devin Review: red/yellow/gray confidence flags (S) https://cognition.com/blog/devin-annual-performance-review-2025 | Vendor: merge rate 34% -> 67% YoY (definition not given). Greptile (independent-ish): Devin reverts 3.50/1k merged PRs vs human 2.72; churn 13.5% vs 10.0%; 2.11 review cycles (fewest) | V |
| Codex | Scheduled automations; findings land in Triage inbox, empty runs auto-archived; standalone (fresh) vs thread automations; project automations need desktop app running; cloud triggers = roadmap (U) | Greptile: lowest reverts 1.19/1k, churn 5.7%, but 2.46 review cycles. AIDev acceptance: Codex top in every cited study | S (docs not read) |
| Cursor | Cloud agents in VMs; Bugbot reviewer -> Autofix spawns cloud agents. Research: flat agents stalled; recursive planners + workers + judge per cycle, fresh start each cycle; locks failed (20 agents ~ throughput of 2-3). https://cursor.com/blog/scaling-agents | Bugbot resolution rate (bugs fixed before merge) 52% -> 76%; >35% of Autofix changes merged (V). Cursor BG PRs: reverts 3.41/1k, P0 flags 0.145/10k LoC (highest of 5, Greptile). Browser 1M LoC/1 wk; Solid->React 3 wk, needs careful review | V |
| Copilot agent / Agent HQ | Issue -> draft PR; Mission Control = one view of Copilot/Claude/Codex sessions; agentic review before human review | dotnet/runtime 10 months (MS devblog V): 878 PRs, 67.9% merged vs 87.1% MS humans; 3/535 merged reverted (0.6%) vs 0.8% non-agent; success 41.7% month 1 -> ~71% latest quarter, mostly from better build/repo instructions (38% -> 69%); 1-50 LoC 76-80% vs 101-500 LoC 64%; humans pushed commits on 52% of merged PRs (86% success with, 55% without); its tests can encode existing bugs; review capacity = emerging bottleneck | V |
| Jules | Plan-first (steps + assumptions), you approve; leave the page and it auto-approves on a timer (V docs). Planning Critic 2026-01, code critic flags only (S) | none found | V/S |
| Amp | Subagents in own context; Oracle second-model reasoner; sources conflict on models/modes | none | S/U |
| Claude Code | Cloud scheduled tasks (ex-routines, research preview): schedule/API/GitHub triggers, claude/ branches, auto mode; prompts not auto-resolved stay pending in the run session; share subscription usage; 100 scheduled runs/h per account, 30/h per task for API/Run-now, no overage on hourly caps (V code.claude.com/docs/en/routines). Auto mode: users accept 93% of prompts; classifier FPR 0.4% (n=10k), FNR 17% on 52 real overeager actions, 5.7% synthetic exfil; headless -p terminates after repeated denials (V anthropic.com/engineering/claude-code-auto-mode). Agent teams experimental, each teammate a full session, not spawned in -p (S) | above | V |

Patterns (inference): every product splits author from checker; plan/contract precedes code; blocked => halt or park; none publishes a structured assumption/decision card; published merge rates have different denominators and are not comparable.
Independent data: AIDev MSR 2026 (arXiv 2602.08915, V abstract): 7,156 PRs; docs 82.1% vs features 66.1% accepted; no agent best across task types. 2602.04226 (S): Codex 85.8, Devin 55.5, Copilot 55.0, Cursor 74.6, Claude 71.3, human 82.6.
Greptile (V, 2026-05-05, vendor with own reviewer, adverse-selection caveat by author): AI-authored merged PRs 0.86% (Feb 2025) -> 27.6% (Apr 2026); no evidence agent PRs are worse overall, the bugs move; review cycles by size: <10 LoC 1.27, 50-199 1.91, 200-499 2.38, 500-999 2.81, 1000+ 3.54.

## 2. Human-in-the-loop for unattended runs

| Practice | Who | Evidence | Verdict |
|---|---|---|---|
| Plan + assumptions before code, async approval | Jules | timer auto-approve = approval silently optional (V) | TAKE, but timeout must park work, not approve |
| Interview to unambiguous BEFORE the night | Factory, Jules | Ambig-SWE (ICLR 2026, arXiv 2502.13069, S): interaction recovers lost performance up to 74%; models fail to tell under- from well-specified tasks, default to not asking | TAKE (human-side spec session) |
| Mid-run blocking question | Start, Mastra | Unblocked (S): a question that blocks on a person is never answered overnight | TAKE only as non-blocking: log question + default assumption, continue parked |
| Assumption log / decision records | practitioner posts (S) | no measured effect, no standard format | TAKE (own format) |
| Confidence-labelled findings | Devin Review, Greptile P0-P2 | no published precision for Devin Review | TAKE for card ordering only |
| Small PRs / stacked diffs | Copilot, Greptile | cycles grow monotonically with size; dotnet 76-80% (<=50 LoC) vs 64% (101-500) | TAKE slice cap |
| Review capacity bottleneck | Faros (vendor, 10k devs, S): +98% merged PRs, PR size +154%, review time +91%, bugs/dev +9%, org delivery flat | dotnet: top 2 reviewers = 36% of feedback (V) | design driver |
| Review-size limits | SmartBear/Cisco 2006 (S) | defect density highest <200 LoC; effectiveness falls after ~60-90 min; 400 LoC is a derived rule of thumb | TAKE as audit budget |
| Per-action approvals | Claude Code | 93% accepted = fatigue (V) | SKIP; audit outcomes |
| Auto-mode classifier overnight | Claude Code | FNR 17% overeager (V) | MAYBE as extra layer under sandbox |

## 3. Spec-writing assistance

| Practice | Evidence | Verdict |
|---|---|---|
| Spec presence => fewer defects (observational) | ESEM 2026 (V abstract, Hill): 88,052 PRs / 119 repos; spec artifacts mostly issue links; defects -0.6pp p=0.133; spec quality predicts neither defects (p=0.164) nor rework (p=0.860); specs proxy complexity; SDD tool output not in sample | do not claim spec = quality |
| Spec Kit/Kiro/BMAD/OpenSpec vs none | Uvik (V, vendor-run, 50 Python tickets, one model, ~250 runs, +10 reruns): merged 72% none vs 80-84% spec; review defects/merged ticket 0.86 vs 0.39-0.57; rework 0.74 vs 0.46-0.64; cost/merged $2.43 none, $2.35 Kiro, $2.71 OpenSpec, $3.33 Spec Kit, $4.23 BMAD; time 29 vs 36-55 min; +-10pt noise band; Kiro in different IDE | MAYBE: light spec cheap; BMAD-weight 1.7x cost, no extra merges. Small, interested party |
| Same spec -> different code across runs (Boeckeler, S) | no numbers | verify behavior, not code identity |
| LLM clarifying questions | HumanEvalComm Okanagan: communication rate +58pp, good-question +38pp (S); ClarifyCoder +40/+30pp (S); LLMREI, ReqElicitGym (U) | TAKE interview; evidence is simulated-user benchmarks |
| EARS / Gherkin | no measured defect effect found | Gherkin only for executability/traceability |
| Property tests from spec | Anthropic agentic PBT (V): 984 reports over 100+ packages; of 50 reviewed 56% valid, 32% reportable; 3 of 5 sent patches merged. PBT-Bench (arXiv 2605.15229, V): 100 problems, 365 injected bugs; recall 31-77% open vs 42-83% with Hypothesis scaffold; +20pp for mid models, 2 models worse | TAKE for stateful invariants; plan for ~half noise |
| Ambiguity lint | ClarifyGPT: disagreement among sampled implementations flags ambiguity (S) | MAYBE: 3 cheap samples per AC |

## 4. Stability of long-lived stateful systems

| Practice | Agent evidence | Verdict |
|---|---|---|
| Hypothesis / stateful PBT | above; PBT-Bench hardest tier = stateful cross-function protocol bugs | TAKE |
| Deterministic simulation + replay | blog only (S): replay easy, choosing interleavings hard; no agent-maintenance study | MAYBE: seed-driven bot sim, human reviews invariant list |
| Agent-written test quality | Beyond Test Presence arXiv 2607.12068 (S): pass rate poor proxy, more flakiness risk patterns, no confirmed flake rates; SQLite study 63% of flaky tests (72/115) assumed unspecified order (S) | TAKE flake quarantine gate |
| Regression ratchet (coverage/mutation/test count never drop) | no 2026 evidence found | TAKE (mechanical) |
| Architecture fitness functions; E2E soak | nothing found for agents | MAYBE (lint rules; nightly script, not agent) |

## 5. Cost / throughput

| Practice | Evidence | Verdict |
|---|---|---|
| Frontier planner + cheap workers | Cursor SQLite-in-Rust (S via The Decoder, vendor, single benchmark): Opus 4.8 planner + Composer 2.5 workers $1,339 vs GPT-5.5 alone $10,565; worker fleet $411; workers >=69% (usually >90%) of tokens; planner ~2/3 of hybrid bill; all configs reached 100% eventually | TAKE tiering |
| Model per role | Cursor (V): GPT-5.2 stayed focused, Opus 4.5 stopped early (version-specific, likely stale) | MAYBE: re-measure on bob-bench |
| Cache dominates | Factory case: 744.9M of 778.5M tokens cache reads (V) | TAKE: stable prefix, fresh short workers |
| Parallel worktrees | 3-5 sessions sweet spot, ~5x burn of 5h window with 5 sessions (S blogs, no primary) | serial default |
| Subscription limits | 5h window + weekly + Sonnet-only weekly on Max (S); hours/promos conflicting (U) | pre-run usage check |
| Agent teams ~15x cost | S, unconfirmed | SKIP overnight |

## 6. Open-source orchestrators

| Tool | Notes | Verdict |
|---|---|---|
| Vibe Kanban | worktree kanban; Bloop cloud shut 2026-04-10, community-run (S) | SKIP (maintenance) |
| Claude Squad | tmux + worktree TUI, AGPL (S) | SKIP |
| Conductor (Melty) | Mac app, parallel Claude/Codex, BYO subscription (S) | MAYBE manual cockpit |
| OpenHands | ~68k stars (U, conflicting); OpenHands Index Jan 2026 (S) | reference only |
| BMAD/Spec Kit/OpenSpec | see section 3 | |
| task-master, ccpm | not found | gap |

No verified star counts or adoption measurements for any orchestrator (listicles only).

## Design implications for overnight spec -> code with morning audit

1. Spend human time BEFORE the night: interview-driven spec session; agents do not self-detect ambiguity (Ambig-SWE). Gate launch on zero open blockers.
2. Write a validation contract first (finite testable assertion ids, Factory); each slice claims ids; morning card shows assertion pass/fail, not diff.
3. Never block overnight on a human: question -> queue with default assumption, continue on parked branch; mark result assumption-based. The Jules timer shows silent auto-approve is the failure mode.
4. Decision card = {question, assumption taken, alternatives, blast radius, revert command, confidence, evidence link}; sort by blast radius x low confidence. Format is our invention; no standard exists.
5. Cap slice at ~100-200 changed LoC (dotnet 76-80% vs 64%; Greptile cycles 1.9 -> 2.4 -> 3.5).
6. Budget audit at <=60-90 min and ~<=400 LoC human-read (Cisco-derived, old); machine gates cover the rest; auto-merge only green low-blast cards to a staging branch.
7. Checker separate from author; validators report only (Factory, Jules); expect 2-4 validate/fix rounds per milestone, ~1/3 of impl as fix work (Factory 34%).
8. Repo instructions are first-class: dotnet success 38% -> 69% from setup instructions alone; bob-init must prove build/test/run work headless.
9. Auto-mode classifier only as extra layer (FNR 17%); rely on worktree/sandbox, deny rules, branch-only push; headless exits after repeated denials, so design resume-from-state-file.
10. Stability beyond unit tests: stateful PBT from invariants, seed-replayable bot sim, flake quarantine, ratchets; budget ~half of agent PBT findings as noise (56% valid).
11. Do not claim spec => fewer defects: observational null (ESEM) vs vendor positive (Uvik); keep specs light and measure on bob-bench.
12. Tier models: strong plans/verifies, cheap implements (workers >=69% of tokens); re-measure each release.
13. Serial by default on a subscription; parallel lanes burn the same 5h/weekly caps; add stop-before-limit rule and per-night budget.
14. Morning report = triage inbox: runs with nothing to report auto-archive (Codex); only decision cards surface.
15. Track per night: merged-card rate (define denominator), reverts per 1k, review cycles, cost per accepted card; vendor numbers are not comparable.

## Gaps / not confirmed

- Cursor Cloud Agents, Amp, Copilot HQ official outcome data; Codex cloud-trigger status; agent-teams official cost.
- Devin Review precision; Greptile is itself a review vendor.
- EARS effect on defects and spec-maintenance cost: none found.
- Agent maintenance of soak/fitness/replay suites: no evidence located.
- Orchestrator adoption; task-master/ccpm/BMAD-loop measurements: not found.
- Faros/METR 2026/DORA 2025 primary pages and Anthropic Agentic Coding Trends PDF not opened (S).
- Plan-limit hours and promo status: conflicting secondaries (U).
