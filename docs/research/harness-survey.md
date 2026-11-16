# Harness survey (2026-11-17)

Legend: V = read from the primary page this session; S = search snippet or secondary only; UNVERIFIED = not confirmed.

## 1. TL;DR
- Anthropic harness post: solo Opus 4.5 = 20 min / 9 USD (core feature broken); full planner+generator+evaluator = 6 h / 200 USD. With Opus 4.6 they dropped sprints and moved the evaluator to ONE pass at the end; the planner stayed because without it the generator under-scoped. [V] https://www.anthropic.com/engineering/harness-design-long-running-apps
- Their rule: every harness component encodes an assumption "the model cannot do this alone"; remove one component at a time and measure. Cutting everything at once failed. [V]
- Multi-agent costs ~15x chat tokens (single agents ~4x); it pays off for parallelizable breadth; "most coding tasks" are a poor fit. [V] https://www.anthropic.com/engineering/built-multi-agent-research-system
- The +90.2% of Opus-lead + Sonnet-subagents was measured on RESEARCH vs single Opus 4. No coding evidence for this split found. [V]
- Claude Code docs: a fresh reviewer helps (has not seen the reasoning); delegating the same task to a subagent does not improve the answer; subagents cost tokens and can be slower. [V] https://code.claude.com/docs/en/best-practices
- Verifier evidence is mostly qualitative: an out-of-the-box evaluator "talks itself into approving"; needs tuning on logs and few-shot calibration. Devin Review: avg 2 bugs per PR, ~58% severe (vendor figure). [V]
- Cognition: keep writes single-threaded; extra agents only contribute intelligence (clean-context reviewer, "smart friend" escalation); parallel-writer swarms are "mostly a distraction". [V] https://cognition.com/blog/multi-agents-working
- Spec ambiguity matches our failures: clarification raised GPT-4 pass@1 70.96 -> 80.8 (MBPP-sanitized, human answers), but a later benchmark finds LLMs ask poor questions (-7.8..-19.8 pts vs full spec). [S] arXiv 2310.10996, 2607.00711
- Spec-driven tools (Spec Kit, Kiro, BMAD) publish NO quality measurements; independent critique says heavy ritual, non-deterministic output. [V + S]
- Hackathon winner (Opus 4.8 Build Day: Tekton): full PRD + ~50 tickets, parallel workflows, isolated verifier sub-agents, 20 tests as the self-correction loop. Common element: hard executable spec. [V]

## 2. Technique table
| Technique | Evidence of quality effect | Token / time cost | Recommend |
|---|---|---|---|
| Single strong agent, one feature at a time, git + progress file | Anthropic initializer/coding-agent post (V); our plain Opus 94-100% at ~1 USD | Lowest | TAKE as base |
| Frozen acceptance tests from the spec BEFORE code (feature-list JSON, only passes mutable) | Anthropic forbids editing tests, JSON harder to overwrite (V). LLM fail-to-pass test generation is weak: best GPT-4o 23.6% on TDD-Bench-Verified (S, arXiv 2412.02883). CodeT: generated tests + agreement +18.8 pts HumanEval, code-davinci-002 (S, arXiv 2207.10397). No clean tests-first vs code-first comparison found | 1 extra pass | TAKE; tests must be spec-traceable; they inherit the ambiguity of the spec |
| Ambiguity detection / clarification before coding | ClarifyGPT +9.8 pts human answers, +7.7 avg simulated (S, arXiv 2310.10996); ClarifyCodeBench: LLM clarification still weak (S). Docs: "interview me until you could write the spec" (V) | One cheap pass | TAKE as a gate producing a decisions list; queue human questions BEFORE leaving |
| One independent end-of-run verifier (clean context, read-only tools, spec vs code, "skip style") | Docs (V); Anthropic evaluator "single pass at end" (V); Devin numbers vendor-only (V) | ~10-20% of run | TAKE (Opus, 1 pass) |
| Per-slice multi-role pipeline (architect, hardener, QA, fresh context each) | Anthropic: ~20x cost (V); Cognition warns on dispersed context (V); our 9-20 USD and time overrun | 10-20x | SKIP |
| Sprint contracts / per-sprint evaluator | Removed by Anthropic for Opus 4.6 (V) | High | SKIP |
| Self-consistency / N samples + selection | Verifier-based Best@k scales ~log-linearly (S, SWE-Gym); SWE-Replay cuts naive TTS cost 17.4% (S). Untested on ambiguity failures; samples share the same misreading | N x | MAYBE for hard slices; not for ambiguity |
| Mutation testing | Meta ACH: 73% generated tests accepted; equivalent-mutant detector P/R 0.95/0.96 with preprocessing (S, arXiv 2501.12862); 100% coverage can mean 4% mutation score (S, MUTGEN). No end-to-end evidence it lifts hidden-test pass rate | CPU, few tokens | MAYBE: deterministic gate on changed files, no LLM role |
| Opus orchestrator + Sonnet/Haiku workers | Research only, +90.2% (V). Aider architect/editor: o1-preview+Sonnet 82.7 vs 79.7 solo; Sonnet+Sonnet 80.5 vs 77.4 (V, 2024 models, edit benchmark) | Fewer Opus tokens if workers do real work | MAYBE: delegate mechanical/noisy work; keep coding in one context. UNVERIFIED for modern coding |
| Spec Kit / Kiro / BMAD | No measurements on official pages (V) | Docs overhead | SKIP tools, steal artifacts (requirements, design, tasks) |
| Hook/skill mega-bundles (ECC, claude-flow/Ruflo) | ECC page: no benchmarks (V). Ruflo 84.8% SWE-bench: no independent reproduction, audits dispute it (S) | Context bloat | SKIP |
| Hard budget/step limits (--max-budget-usd, iteration caps) | "Stopping conditions" advice (V); flag in headless docs (V) | Free | TAKE |
| Ralph method (G. Huntley: same prompt re-fed to a fresh agent, one item per pass, tests reject bad output) | Anecdotes only: 297 USD vs a 50k contract (V, self-reported); author warns it may not compile; greenfield only | Many passes | MAYBE: copy the file contract (specs/, plan, one item per pass), not the unbounded repetition |

## 3. Notes per source
- Building effective agents (V): five workflows; start simple; ground truth from the environment each step; max-iteration stops. https://www.anthropic.com/research/building-effective-agents
- Multi-agent research system (V): 4x/15x tokens; token usage explains 80% of BrowseComp variance; effort-scaling rules; detailed delegation briefs. https://www.anthropic.com/engineering/built-multi-agent-research-system
- Effective harnesses for long-running agents (V): initializer writes init.sh, progress file, first commit, 200+ feature JSON (all failing); one feature per session; E2E browser testing beat unit/curl checks; open question whether multi-agent beats single. https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
- Harness design for long-running apps (V): evaluator tuning took several rounds; DAW run on Opus 4.6 = 3h50m, 124.70 USD (build 113.85, QA 10.39, planner 0.46). Snippets disagree on post date (Mar 24 vs Apr 4 2026): UNVERIFIED.
- Claude Code best practices (V): give Claude a check to run; interview -> SPEC.md -> fresh session; /code-review in a fresh subagent; "a reviewer asked to find problems will always find something", so scope it; Haiku for simple subagents; cache ~1 h on subscription, 5 min for subagents. https://code.claude.com/docs/en/best-practices
- Subagents docs (V): model alias or CLAUDE_CODE_SUBAGENT_MODEL; nesting up to 3 levels by default; 20 concurrent; non-fork subagent starts without history. https://code.claude.com/docs/en/sub-agents
- Headless / Agent SDK (V): claude -p, --output-format json gives total_cost_usd, --json-schema, --resume, --max-budget-usd, --permission-mode auto --permission-prompts none. --bare does not use the subscription login (needs API key): unsuitable for a subscription-limit plan. https://code.claude.com/docs/en/headless
- Auto mode (V): classifier misses 17% of real overeager actions; 3 consecutive / 20 total denials end a headless run. https://www.anthropic.com/engineering/claude-code-auto-mode
- Hackathons (V): two Anthropic events found; which is "most recent" is UNVERIFIED (no dates on pages).
  - Opus 4.8 Build Day: 1st Tekton (Holly Tang, Austin Burgess) https://github.com/tangxiya-star/Tekton ; 2nd Sim Francisco; 3rd Custom Universe. https://claude.com/resources/articles/meet-the-winners-of-our-claude-opus-4-8-build-day-hackathon
  - Built with Opus 4.6: 1st CrossBeam (prompted, had Claude write tests), 2nd Elisa (spec -> meta-planner task graph, 1500+ tests, ~30 h). https://claude.com/resources/articles/meet-the-winners-of-our-built-with-opus-4-6-claude-code-hackathon
  - Everything Claude Code (affaan-m): hackathon win reported only by secondary sources (Feb 2026; sources conflict on solo vs two winners); repo page has no hackathon mention and no benchmarks. https://github.com/affaan-m/everything-claude-code . Not verified as Anthropic-run.
  - Winners repos not inspected; harness internals beyond the articles UNVERIFIED.
- Aider architect/editor (V): +2..5 pts on the 2024 edit benchmark (edits, not spec compliance). https://aider.chat/2024/09/26/architect.html
- Ralph (V): https://ghuntley.com/ralph/
- Spec Kit (V): constitution, specify, plan, tasks, implement, converge. https://github.com/github/spec-kit
- Kiro (V): requirements/design/tasks files, parallel waves, vendor claims without data. https://kiro.dev/docs/specs/
- BMAD (V): Clarify, Plan, Build-and-verify, Learn; has an unattended Loop module; no evidence. https://github.com/bmad-code-org/BMAD-METHOD
- Boeckler/Fowler on SDD (S only, page not opened): https://www.martinfowler.com/articles/exploring-gen-ai/sdd-3-tools.html
- Cognition, Do not Build Multi-Agents (S): https://cognition.com/blog/dont-build-multi-agents
- SWE-bench scaffolds (S): mixed, no controlled scaffold comparison found. mini-SWE-agent (bash only) >70% Verified https://www.swebench.com/bash-only.html ; Agentless 27.3%/0.34 USD (v1) vs 32%/0.70 USD (later), version UNVERIFIED https://arxiv.org/abs/2407.01489 ; contamination concerns on Verified (S).
- OpenHands (S): V1 modular SDK, event-sourced; exact scores UNVERIFIED. https://arxiv.org/abs/2511.03690
- SWE-agent: only via mini-SWE-agent above.
- Self-preference (S): documented, but one study finds strong models prefer own output mostly because it is better; no code-review-specific verdict.
- Not found: tests-first vs code-first comparison with modern models; coding evidence for Opus-orchestrator+Sonnet-worker; token cost of LLM mutation runs; winners repo internals.

## 4. Harness sketch (derived hypotheses, not proven)
1. Phase 0 (Opus, once): read the yamlkit spec; write decisions.md = each ambiguity + chosen interpretation + rationale; blocking questions go to the human BEFORE the run.
2. Phase 1 (Sonnet draft, Opus review): acceptance.json (id, spec-clause ref, steps, passes:false) + executable tests tagged by clause ID; Opus checks every clause is covered.
3. Freeze tests (permission deny + checksum); only passes may change.
4. Phase 2 (ONE Opus context): implement item by item: red, green, build/lint/format gates, commit, update progress.md. One item per pass; auto-compaction, no per-role resets.
5. Delegate to Haiku/Sonnet (model field) only noisy or mechanical work: test runs, repo search, bulk edits, extra edge-case tests. Design and code ownership stay in the Opus context.
6. Deterministic gates between items: build, tests, analyzers, optional mutation run on changed files (no LLM).
7. Phase 3 (Opus, fresh context, read-only tools, ONE pass): diff vs spec + decisions.md + frozen tests; "correctness gaps only, skip style"; few-shot calibrated on our past misses.
8. Phase 4: fix confirmed gaps; max 2 rounds; stop.
9. Limits: --max-budget-usd, step cap, wall-clock deadline; per-phase total_cost_usd log.
10. Evaluation: ablate ONE component at a time vs the plain-Opus baseline on the same 3 spec tasks plus ambiguity-seeded specs; keep a component only if it beats run-to-run noise (N>=3 per arm; sufficiency UNVERIFIED).
11. Expected cost 2-4x plain Opus (estimate extrapolated from Anthropic DAW breakdown, QA ~8%); effort goes to the spec step (our observed failure), not extra roles.

## 5. Gap fill (2026-11-17)

Legend as above. V = fetched page this session (WebFetch returns a model-written summary of the page, not raw text); S = search snippet; UNVERIFIED = not confirmed.

### 5a. Notes per gap

**G1. Hackathon winners (repos opened)**
- Recency: Opus 4.8 Build Day = held 2026-06-13, article 2026-06-17 [V] https://claude.com/resources/articles/meet-the-winners-of-our-claude-opus-4-8-build-day-hackathon . Opus 4.6 hackathon = article 2026-04-20; event dates not on page; CrossBeam README says February 2026 snapshot [V] https://claude.com/resources/articles/meet-the-winners-of-our-built-with-opus-4-6-claude-code-hackathon . Most recent of the two found: Build Day. Newer Anthropic events not searched: UNVERIFIED.
- Tekton (1st, Build Day) [V] https://github.com/tangxiya-star/Tekton : repo has PRD.md, done.rubric.json, scripts/verify.mjs, orchestrate.mjs. README names no model version.
  - Roles per README: parallel READ-ONLY diagnosers, SEQUENTIAL implementers (so edits do not collide), adversarial verifier.
  - done.rubric.json (30 binary criteria, 22 required, 7 critical; no numeric score): orchestrate.mjs grades each from real artifacts after VERIFY and refuses to enter SHIP unless all required are green; "done is decided by the verifier, not a human". https://raw.githubusercontent.com/tangxiya-star/Tekton/main/done.rubric.json [V]
  - Verifier: deterministic script recomputes 12 geometry checks from component coordinates, NOT from the claimed spec values (check V09: roof ratio 1:2.67 not 1:3); failed reports kept; npm run build exits nonzero on failed verify; demo:corrupt/restore shows it rejects bad output. Plus a vision-verifier sub-agent in fresh context, one criterion each. No unit-test suite described.
  - Takeaway: executable done-rubric + deterministic verifier that distrusts the spec numbers + single-writer implementation. Mapping the article "20 tests" to this verifier is my inference.
- CrossBeam (1st, Opus 4.6) [V] https://github.com/mikeOnBreeze/cc-crossbeam : "hackathon snapshot, not the current product"; Agent SDK claude_code preset in Vercel Sandboxes; domain Skills (28 reference files + decision-tree router); no models named; no test framework or verification loop in README. Article: author had Claude write tests, "did not read a line of code". Harness internals beyond this UNVERIFIED.
- Elisa (2nd, Opus 4.6) [V] https://github.com/zoidbergclawd/elisa : NuggetSpec (Zod-validated JSON) is the single source of truth; metaPlanner decomposes it into a task DAG; up to 3 agents in parallel with dependency order and retries; phases plan/execute/test/deploy; testRunner runs pytest/Node tests; "Fix It" = focused repair task + re-run. MODELS: builder, tester, reviewer all default claude-opus-4-6; only the narrator is claude-haiku-4-5. No Sonnet workers.
- Other placings (PostVisit, TARA, Conductr; Sim Francisco, Custom Universe): workflow not described on the article pages [V]; Sim Francisco used a verifier agent + adversarial agent (article).

**G2. Strong plans / cheap implements (recent evidence)**
- ManagerWorker, SWE-bench Lite, 200 inst. [V abstract] https://arxiv.org/abs/2603.26458 : strong manager + weak worker 62% vs strong single 60%; weak+weak 42% vs weak single 44%; review-only loop ~+2 pts; structured exploration/planning ~+11 pts. Token/cost numbers not on abstract (UNVERIFIED). Authors argue director/worker splits go against monolithic training. Models unnamed on abstract.
- Harness study, 4 models, SWE-bench Verified + Terminal-Bench 2.1 [V abstract] https://arxiv.org/abs/2609.20804 : planning = accuracy aid for weak models, "a cost saver for stronger models" with little accuracy change. Cost ~30% only via snippet [S].
- Aider polyglot [V, page last updated 2025-11-20] https://aider.chat/docs/leaderboards/ : o3(high)+gpt-4.1 architect/editor 78.2% / 17.55 USD; DeepSeek R1 + claude-3.5-sonnet 64.0% / 13.29 USD; single gpt-5(high) 88.0% / 29.08 USD, gpt-5(medium) 86.7% / 17.69 USD. The best pair is BELOW the best single model; no Opus+Sonnet row (UNVERIFIED for any 2026 model).
- Claude Code opusplan: Opus in plan mode, Sonnet in execution [V] https://code.claude.com/docs/en/model-config . Exists; no published quality/cost numbers found.
- Net: modest evidence (+2 pts, SWE-bench Lite, unnamed models); no controlled Opus-vs-Sonnet worker study on spec-driven coding.

**G3. Mutation testing in LLM loops**
- SWE-ABS [V abstract] https://arxiv.org/abs/2603.00520 : mutation-driven adversarial patches + coverage augmentation strengthened tests on 50.2% of 500 SWE-bench Verified instances; rejected 19.71% of passing patches; top agent 78.80% -> 62.20%. Shows weak tests inflate pass rates; a benchmark-hardening tool, not an agent loop.
- SWE-Mutation (2026): LLM-written test suites detect ~39.81% of mutants on average (best 36.15%); pass rate is a weak signal [S] https://benchmarklist.com/benchmarks/swe_mutation_can_llms_generate_reliable_test_suites_in_software_engineering/ .
- Meta ACH [S] https://arxiv.org/abs/2501.12862 : 73% acceptance, Llama 3.1 70B; mutant counts conflict between sources; NO token/cost figures found.
- Still not found: tokens/time of mutation inside an agent loop; any paper showing mutation feedback raises HIDDEN-test pass rate of generated code.

**G4. Tests-first vs code-first; ambiguity**
- Risk of coding before testing [V abstract] https://arxiv.org/abs/2607.05139 : tests generated after faulty code detect 14% of faults vs 25% when generated independently (error propagation). Closest direct evidence for tests-from-spec BEFORE code. Models and mutation scores not on abstract.
- Mathews and Nagappan [S] https://arxiv.org/abs/2402.13521 : giving human tests to GPT-4 solves +12.0% (MBPP) / +8.5% (HumanEval) more problems.
- Ambig-SWE (ICLR 2026) [V abstract] https://arxiv.org/abs/2502.13069 : interaction improves underspecified-task performance by up to 74%; models struggle to tell under- from well-specified inputs (Sonnet 3.5 84% per snippet [S]). Interaction needs a user.
- Without a human: ClarifyGPT consistency check (sample programs, run on generated inputs, divergence = ambiguous) [S] https://arxiv.org/abs/2310.10996 ; automated problem-description repair [S] https://arxiv.org/abs/2505.07270 (numbers not seen). Snippet: direct "clarify yourself" prompting gives irrelevant edits.
- Inference: autonomous ambiguity resolution = detect via divergence across N interpretations, then record a decision; it cannot be validated without a human oracle.

**G5. Best-of-N / LLM verifier**
- Agentic Rubrics [V abstract] https://arxiv.org/abs/2601.04171 : execution-free rubric verifier, parallel TTS on Qwen3-Coder-30B 54.2%, >= +3.5 pts over best baseline; no cost figures. Rubrics also flag issues tests miss.
- R2E-Gym: hybrid execution + execution-free verifier scales better than either [S] https://arxiv.org/abs/2504.07164 . R4P patch verifier 26.2 -> 32.8 pass@1, ~50x faster than testing [S] https://arxiv.org/abs/2510.22775 .
- SWE-ABS (G3) implies a selector built on weak tests inherits their blind spot.
- All numbers are open-weight/small models; no 2026 frontier-model verifier effect size with cost found.

**G6. OpenHands, SWE-agent**
- OpenHands SDK [V abstract] https://arxiv.org/abs/2511.03690 (MLSys 2026): event-sourced, modular, sandboxed, multi-LLM routing, built-in security analysis; "strong performance" with NO numbers on the page. Repo page has no scores [V] https://github.com/OpenHands/OpenHands . Current verified score: NOT obtained.
- SWE-agent [V] https://github.com/SWE-agent/SWE-agent : README says development moved to mini-SWE-agent ("use mini-SWE-agent instead"); mini-SWE-agent 65% Verified (July, year not stated). swebench.com leaderboard rows did not render; bash-only URL 404: current scores UNVERIFIED.

**G7. Claude Code unattended runs**
- Skills [V] https://code.claude.com/docs/en/skills : frontmatter context: fork + agent + model + effort + allowed-tools; forked skill gets no conversation history, background by default (background: false to wait); Explore/Plan agents skip CLAUDE.md.
- Headless [V] https://code.claude.com/docs/en/headless : --resume <id> / --continue; --permission-prompts none (v2.1.259+; AskUserQuestion removed); SIGTERM = exit 143, turn unfinished, CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1 to continue; background work waits max 10 min at exit (CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS); api_retry stream events; -p starts in Manual mode when nothing sets a mode (v2.1.302+).
- Rate limits [V] https://code.claude.com/docs/en/errors : 10 retries by default; CLAUDE_CODE_RETRY_WATCHDOG=1 retries 429/529 indefinitely, with RETRY_WATCHDOG_MAX_WAIT_MS, ..._MAX_USAGE_LIMIT_WAIT_MS, ..._USAGE_LIMIT_RECHECK_MS (v2.1.295/301/305+); a spend-limit 429 fails at once. Session-limit resume section not read (page truncated): UNVERIFIED.
- Budget [V] https://code.claude.com/docs/en/agent-sdk/cost-tracking : max_budget_usd counts only the call own spend (resumed totals NOT counted, /clear resets it), so multi-resume runs need our own cumulative cap; error_max_budget_usd result: total_cost_usd includes the crossing response, usage does not; modelUsage = per-model cost incl. subagents; total_cost_usd is a client-side estimate; resumed result reports whole-session total (do not sum). Cache TTL 5 min on API key by default (ENABLE_PROMPT_CACHING_1H).

### 5b. Technique table rows (new / updated)
| Technique | Evidence of quality effect | Token / time cost | Recommend |
|---|---|---|---|
| Executable done-rubric + deterministic verifier recomputing from artifacts, not from spec claims (Tekton) | 1st at Build Day; gate blocks SHIP; no ablation (V, repo) | Script CPU; one fresh-context agent per vision criterion | TAKE (extends our E1/G1 gates) |
| Single-writer implementation, parallel read-only diagnosers (Tekton) | Matches Cognition (V); no ablation | Low | TAKE |
| Tests generated BEFORE code, independent of implementation (UPDATE) | Post-code tests detect 14% vs 25% of faults (V abstract, 2607.05139); human tests +8.5..12 pts (S) | 1 extra pass | TAKE (strengthened) |
| Ambiguity: divergence check across N interpretations -> decisions.md (UPDATE) | ClarifyGPT 62.4 -> 69.6 avg (S, needs Q&A); LLMs poor at self-detecting (Ambig-SWE, V) | N cheap samples + test inputs | MAYBE; human question queue before leaving stays the real fix |
| Strong manager + cheap worker (UPDATE) | Lite 62 vs 60 strong-solo; weak+weak worse (V abstract); planning = cost saver for strong models (V abstract); Aider best pair < best single (V) | Fewer strong tokens; no USD in abstracts | MAYBE (narrow benefit confirmed) |
| Mutation score as agent gate (UPDATE) | No hidden-test lift shown; SWE-ABS: weak tests inflate pass by ~16 pts (V abstract) | CPU; cost per run not found | MAYBE: offline audit of the frozen test suite, not per slice |
| Execution-free rubric / LLM verifier over N candidates | +3.5 pts over baselines on Qwen3 30B (V abstract) | Extra judge calls, unquantified | SKIP for now (open-weight evidence only) |
| Forked skill with model/effort for the reviewer | Docs only (V) | One subagent | TAKE as mechanism for the single verifier |
| RETRY_WATCHDOG + own cumulative budget cap across resumes | Docs only (V) | Free | TAKE |

### 5c. Implications for forge design
- Hackathon winners give NO evidence for Opus-orchestrator + Sonnet-workers: Elisa runs builder/tester/reviewer on Opus 4.6 and only narration on Haiku; Tekton names no model. Keep Opus-main + delegated mechanical work as MAYBE, not a proven default.
- Copy the Tekton contract: machine-gradable done rubric, an orchestrator SCRIPT (not the model) decides SHIP, verifier recomputes from artifacts and distrusts spec-stated values, failed reports retained.
- Single final verifier is not contradicted, but the Tekton decisive gate is deterministic; an LLM-only verifier inherits the weak-test blind spot (SWE-ABS), so keep deterministic gates behind it.
- Generate acceptance tests from the spec in a context that has not seen the implementation (14% vs 25% detection); freeze them.
- Ambiguity: no reliable autonomous resolver. Queue questions before leaving; log the rest in decisions.md with N-sample divergence evidence and have the verifier check each decision.
- Mutation: no per-slice runs on current evidence; run once offline on frozen tests to measure suite strength.
- Unattended: RETRY_WATCHDOG=1 with max wait, --permission-prompts none, own cumulative spend cap (max_budget_usd ignores resumed spend), resume by session id; no --bare on a subscription plan.
- Forked-skill reviewers: background: false and all context inside SKILL.md.
