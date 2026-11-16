# Multi-agent software development: evidence for high-quality autonomous code (2026-11-17)

Legend: V = primary page/abstract read this session; S = search snippet or secondary; U = UNVERIFIED. Numbers copied, never inferred.
Scope excludes (covered elsewhere): hackathon repos, Aider architect/editor, mutation testing, ClarifyGPT, OpenHands/SWE-agent scores.

## 1. Practice -> evidence -> cost -> verdict

| # | Practice | Evidence (effect, source) | Cost | Verdict |
|---|---|---|---|---|
| 1 | Single-threaded writer (one agent commits all edits); others only read/advise | Cognition: "multi-agent systems work best today when writes stay single-threaded"; parallel writers make conflicting implicit decisions (V: https://cognition.com/blog/dont-build-multi-agents , https://cognition.com/blog/multi-agents-working). Anthropic: coding has fewer truly parallelizable tasks than research (V: https://www.anthropic.com/engineering/multi-agent-research-system). Claude Code docs: same-file edits and sequential work -> single session (V: https://code.claude.com/docs/en/agent-teams). Warp (71% Verified) tried multi-agent, kept one primary agent (S: https://arxiv.org/pdf/2506.17208) | 1x | TAKE |
| 2 | Parallel writers on disjoint files / worktrees | Docs allow it (each teammate owns different files; isolation: worktree) but advise starting with research/review (V: agent-teams, sub-agents docs). Google scaling study: independent MAS amplify errors up to 17.2x vs 4.4x centralized (V: https://research.google/blog/towards-a-science-of-scaling-agent-systems-when-and-why-agent-systems-work/). No coding-specific measured gain found | ~7x tokens with plan-mode teams (V: https://code.claude.com/docs/en/costs) | MAYBE (hard file ownership + orchestrator-only merge) |
| 3 | Fresh-context reviewer/validator separate from implementer | Factory: implementer is worse at evaluating own work than a fresh reviewer (V: https://factory.com/news/missions-architecture). Cognition Devin Review: ~2 bugs/PR, ~58% severe, best without coder context (V: multi-agents-working; vendor self-report, no baseline). Claude docs: fresh reviewer helps, delegating same task to subagent does not (V: https://code.claude.com/docs/en/best-practices) | Factory mission: validation 263.8M of 778.5M tokens (34%), 37.2% of time (V) | TAKE |
| 4 | Verifier = executable checks (tests, black-box run) | Anthropic: coding suits agents because verifiable by tests (V: https://www.anthropic.com/engineering/building-effective-agents). Harness post: agents mark done after unit tests/curl; browser E2E substantially improved results (V: https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents). MAST: task-objective verification step +15.6% on ProgramDev (V via html: https://arxiv.org/html/2503.13657v3) | low | TAKE |
| 5 | LLM-as-judge as sole correctness gate | Best of 8 judges (GPT-4-turbo) "frequently misjudges" correctness (V abstract: https://arxiv.org/abs/2507.16587). Patch-correctness study: triage only, not oracle; telling judge "all tests pass" inflates false positives (S: ESEM 2026 https://drops.dagstuhl.de/storage/00lipics/lipics-vol394-esem2026/LIPIcs.ESEM.2026.21/LIPIcs.ESEM.2026.21.pdf). Over-critique -> false negatives (S: https://arxiv.org/pdf/2508.12358). Claude docs: a reviewer asked to find problems always finds something (V) | 1 call | SKIP as sole gate; MAYBE triage + severity filter |
| 6 | Test suites are fallible verifiers; strengthen them | UTBoost: augmented tests flagged 15.7% (92/584) of Verified-passing patches as wrong; 26/500 instances have insufficient tests (S: https://arxiv.org/pdf/2506.09289, own-tool result by the authors) | extra test-gen | TAKE |
| 7 | Independent test-writer separate from code-writer (AgentCoder) | GPT-4: 96.3% HumanEval / 91.8% MBPP at ~56.9K/66.3K tokens vs prior SOTA 90.2%/78.9% at ~138.2K/206.5K (V abstract: https://arxiv.org/abs/2312.13010). Caveat: saturated/contaminated benchmarks, GPT-4 era, self-run comparison | lower than baselines per paper | TAKE principle, discount magnitude |
| 8 | Role-play SDLC chains (MetaGPT/ChatDev) | MetaGPT: "more coherent" than chat MAS, no numbers (V: https://arxiv.org/abs/2308.00352). ChatDev abstract: no metrics (V: https://arxiv.org/abs/2307.07924). MAST: 7 open-source MAS fail 41%-86.7%; ChatDev role clarification +9.4% (V via html summary) | high chatter | SKIP role theatre; keep SOP/structured artifacts |
| 9 | Planner + coder + debugger stages (MapCoder) | 93.9 HumanEval / 83.1 MBPP / 22.0 APPS / 28.5 CodeContests pass@1; models/baselines not in abstract (V: https://arxiv.org/abs/2405.11403) | 4 agent stages | MAYBE (competitive-programming, not repo-level) |
| 10 | Multi-agent debate | MAD "does not reliably outperform" self-consistency/ensembling; tuned agreement can beat them (V: https://arxiv.org/abs/2311.17371). Claude docs use adversarial hypotheses for debugging (V, no measured effect) | N rounds x N agents | MAYBE root-cause hunting; SKIP for generation |
| 11 | Read-only parallel research/exploration subagents | Anthropic: Opus-4 lead + Sonnet-4 subagents beat single Opus-4 by 90.2% on internal research eval; up to 90% time cut (V: multi-agent-research-system). Domain is research, not coding | ~15x chat tokens vs ~4x single agent (V) | TAKE for recon/scout; coding effect not shown |
| 12 | Strong orchestrator + cheaper workers | Anthropic lead/sub split (V). Sonnet-4 upgrade gave larger gain than doubling Sonnet-3.7 budget (V). Cognition: weak primary calling strong Smart Friend underperformed (did not know when to escalate); cross-frontier routing better (V). Claude docs: Sonnet for teammates, haiku for exploration (V). Factory assigns roles by strength, no models named (V) | saves worker tokens | TAKE strong planner + scoped cheap workers; SKIP weak model deciding escalation |
| 13 | Fresh context per task + external state (feature list JSON, progress file, git) | Harness post: initializer + coding agent, 200+ features all failing, one feature/session, commit each (V). Factory: worker per feature, fresh context, 21 fix features = 34.4% of impl. work (V) | cheap | TAKE |
| 14 | Spec/validation contract before implementation | Factory: orchestrator writes validation contract first so it is not shaped by implementation (V). Claude docs: SPEC.md then fresh session to build (V) | low | TAKE |
| 15 | Best-of-N / tournament at test time | Opus-4.5 SWE-bench Verified 70.9 -> 77.6 (mini-SWE-agent), Terminal-Bench 46.9 -> 59.1 (V abstract: https://arxiv.org/abs/2604.16529). SWE-Lego TTS@16: 8B 42.2->49.6, 32B 52.6->58.8 (S: https://arxiv.org/html/2601.01426). R2E-Gym hybrid verifier 51.0% at 26 rollouts; each verifier alone ~42-43% (S: https://arxiv.org/pdf/2504.07164) | N x full run | MAYBE (hard slices, executable selector) |
| 16 | Fixed pipeline instead of agent loop | Agentless 32.00% SWE-bench Lite at USD 0.70 beat open-source agents of its time (V: https://arxiv.org/abs/2407.01489); old-model era | very low | MAYBE (localize -> repair -> validate as worker design) |
| 17 | Deep management hierarchy | Factory: one layer typical, two for large, three = bureaucracy (S, search paraphrase) | grows | SKIP >2 layers |
| 18 | Free-form swarms / peer chatter | Cognition: swarms mostly fail (V). Anthropic failure modes: duplicated work, excessive updates, 50 subagents for simple query (V). MAST FC2 misalignment ~32% of failures (V via html summary) | high | SKIP |

## 2. Cross-cutting numbers

- Google scaling study (V https://arxiv.org/abs/2512.08296): 260 configs, 6 benchmarks, 3 LLM families. Vs single agent: +80.8% (decomposable finance) to -70.0% (sequential planning). Diminishing returns once single-agent baseline strong. Tool-heavy tasks pay overhead. Centralized verification limits error propagation. No coding benchmark in results (V blog).
- MAST (V https://arxiv.org/abs/2503.13657): 1,600+ traces, 7 frameworks, 14 failure modes in 3 groups (design, misalignment, verification); kappa 0.88. Prompt-level fixes insufficient, structural change needed (V via html summary).
- Anthropic: token usage alone explained 80% of BrowseComp variance (V). Part of any multi-agent gain is more tokens; control for budget.
- Cost multipliers: agent ~4x chat, multi-agent ~15x (V Anthropic); Claude Code teams ~7x with plan-mode teammates (V costs doc).
- SWE-bench landscape (S): Vals.ai board led by bash-only single-agent mini-SWE-agent runs (page archived, benchmark called saturated); Agyn multi-agent +7.2% vs GPT-5-medium+shell is self-reported (S: https://hn.jakechampion.name/reader/https://agyn.io/blog/we-tested-ai-team-swe-bench-verified). Scale SWE-bench Pro: scores depend on turn limit (50 -> 250 turns) per one blog (S: https://www.morphllm.com/swe-bench-pro); claim that an OpenAI audit found ~30% of Pro tasks broken is single-source (U).
- Benchmark caveats: HumanEval/MBPP saturated; Verified tests weak (UTBoost); vendor Pro scores not comparable to Scale standardized runs (S).

## 3. Consensus vs disagreement

Consensus (>=3 independent primaries):
- Simplest thing first; add agents on measured need (Anthropic BEA, Claude docs, Cognition; OpenAI guide S only).
- Coupled writes stay single-threaded; breadth work (research, review, hypothesis search) parallelizes (Anthropic, Cognition, Claude docs, Google study).
- Verification external to author, ideally executable (Anthropic harness, Factory, Claude docs, MAST FC3).
- Fresh context per unit of work + externalized state beats one growing context (Anthropic harness, Factory).
- Multiplier is large (4x-15x), justified only by task value (Anthropic, Claude docs).

Disagreement / open:
- Anthropic (+90.2%, research) vs Cognition ("don't build", coding): reconciled by task coupling (inference, not stated by either).
- Specialized QA/test agents vs one general coding agent: Anthropic harness post lists as open (V).
- Parallel workers on disjoint modules: docs endorse; Cognition/Warp against; no controlled study (gap).
- Factory article: serial workers, validation after milestone, parallelism not discussed (V); product page claims parallel droids (S).
- OpenAI manager vs decentralized: secondary summaries only; primary PDF unreadable via fetch (S).
- Amp: oracle = read-only stronger model for review/advice; cited bottleneck is human review (S only).
- Google ADK: Sequential/Parallel/Loop agents; parallel branches share session state, race risk (S). No Jules guidance found.

## 4. Ideal architecture per evidence

- One strong orchestrator: writes spec + validation contract BEFORE code, cuts small slices, does not write bulk code. (#13, #14)
- Code-defined workflow (state machine) around LLM steps, not free-form agent chat. (BEA, Agentless, Factory runner)
- Single writer per slice: cheaper model, fresh context with only slice spec, contract, conventions; TDD; commit per slice. (#1, #13)
- Slices serial by default; parallel only for disjoint files in worktrees with orchestrator-only merge, after a measured serial baseline. (#2)
- Read-only scouts (haiku/sonnet) in parallel for recon, returning path:line digests. (#11)
- Test author in separate context from implementer; hardener pass targets weak-test gaps. (#6, #7)
- Gate order: executable (build, tests, black-box E2E as a user) first, LLM review second; LLM verdict never overrides red test and is never sole green. (#4, #5)
- Reviewer: fresh context, read-only tools, skip style, severity-rated findings; orchestrator triages and creates fix slices. (#3)
- Fix loops capped (Factory milestones converged in 2-4 rounds) then escalate to human. (V Factory)
- Budget validation at roughly 1/3 of tokens/time (one Factory mission; do not over-generalize).
- State on disk: slice list JSON with pass flag writable only by gate, progress file, git; session start re-runs smoke test. (V harness)
- Best-of-N with executable selector only for hard slices after repeated failure. (#15)
- Max 2 management layers; no peer chatter; each subagent gets goal, output format, boundaries. (#17, #18, Anthropic)
- Log tokens per role/slice; compare against single-agent baseline at equal token budget on own bench before keeping any role. (80% variance finding)

## 5. Open questions / gaps

- No controlled repo-level multi-agent vs single-agent study at equal model AND equal token budget found (U).
- OpenAI guide, Google ADK/Jules, Amp, Factory docs: snippets or partial reads only.
- MAST per-category percentages come from fetch-tool summary of the html (sums are the summarizer's); verify in paper before citing.
- Cognition follow-up date (April 2026 per commentary) not confirmed on the page (U).
- SWE-bench Pro audit, Verified contamination studies, Zencoder 70% ensemble: not verified.
- CodeMonkeys, DeepSWE, SWE-Gym own papers not read.
- Model names in some snippets (Opus 5, GPT-5.6) from search results; treat as S.
- Devin Review 58% severe stat has no baseline or false-positive rate.
