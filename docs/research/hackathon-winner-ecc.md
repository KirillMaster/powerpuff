# Hackathon winners and their agent setups: ECC (affaan-m) and Tekton

Date of research: 2026-11-17. Marks: V = primary read, S = secondary, U = unverified.
ECC local clone: `ecc` (VERSION 2.2.3, HEAD 76aaa77). Repo now `affaan-m/ECC` (renamed from everything-claude-code).

## 1. Who won what

| Person / repo | Event | Date | Placing | Verification | Source |
|---|---|---|---|---|---|
| Affaan Mustafa + David Rodriguez, project Zenith (zenith.chat); repo ECC | Anthropic x Forum Ventures hackathon, "Agentic AI for Zero-to-One", NYC | 2025-09-12 | 1st, 100+ participants, $15k credits | V for event+date (forumvc.com page); win = self-claim only (ECC README:2137; affaanmustafa.com "won first place with my teammate"); S for $15k/100+ (36kr, gentic.news, pulse.bot); no organiser/Anthropic winners page found | https://www.forumvc.com/forum-ventures-x-anthropic-ai-hackathon ; `ecc/README.md:2137` ; https://affaanmustafa.com |
| Same author, AgentShield (security scanner for agent configs) | "Claude Code Hackathon (Cerebral Valley x Anthropic, Feb 2026)" = Built with Opus 4.6 | Feb 2026 | Not a winner: Anthropic list = CrossBeam, Elisa, PostVisit.ai, TARA, Conductr | V: README says "built at", not "won"; V: winners page lacks him | `README.md:2039` ; https://claude.com/resources/articles/meet-the-winners-of-our-built-with-opus-4-6-claude-code-hackathon |
| Holly Tang + Austin Burgess, Tekton | Claude Opus 4.8 Build Day, SF, 12 h, 310 participants (event guide in repo is titled "Claude Fable 5 Build Day": naming mismatch, U which is correct) | 2026-06-13, article 2026-06-17 | 1st | V Anthropic page lists Tekton 1st + repo link; "Cerebral Valley" and "$100K credits" appear only in Tekton README (self-claim; Anthropic page does not name CV) | https://claude.com/blog/meet-the-winners-of-our-claude-opus-4-8-build-day-hackathon ; Tekton README |

Conclusion: "recent winner who published an agents setup" fits **Tekton** (June 2026, verified 1st) better by recency. "Published a full agents setup" fits **ECC** (Sep 2025 win, about 14 months old). The ECC win is real by the author claim and consistent secondary reports, but I found no organiser/Anthropic primary confirming it. The popular "ECC = Cerebral Valley winner" is a conflation (the CV event produced AgentShield, not a placing).

Repo facts (V, gh api): ECC 304,422 stars, 44,837 forks, created 2026-01-18, pushed 2026-11-16. Tekton 38 stars, created 2026-06-12.

## 2. ECC setup (V, local clone)

Inventory: 71 agents, 97 commands, 311 skills (`ls`), hooks in `hooks/hooks.json`. Mostly breadth (language reviewers, resolvers, non-dev roles like marketing/network/homelab), not relevant to the harness.

| Aspect | What ECC does | Source |
|---|---|---|
| Agent roles / models | planner=opus, architect=opus (planner read-only `Read,Grep,Glob`); tdd-guide, code-reviewer, build-resolvers, language reviewers=sonnet; doc-updater, docs-lookup=haiku; most of 71 are sonnet | `CATALOG-agents.md:5,53,70,11,23`; `agents/planner.md` frontmatter |
| Tool limits | Frontmatter `tools:` per agent; loop-operator `Read,Grep,Glob,Bash,Edit` | `agents/loop-operator.md:4` |
| Model routing | Heuristic only: haiku mechanical, sonnet default, opus architecture/ambiguity; `/model-route` recommends, does not enforce | `commands/model-route.md`; `skills/cost-aware-llm-pipeline` |
| Orchestration patterns | 6 documented: sequential `claude -p` pipeline; REPL; infinite loop; Continuous-Claude PR loop (cross-iteration `SHARED_TASK_NOTES.md`); de-sloppify (separate cleanup pass); Ralphinho RFC DAG | `skills/autonomous-loops/SKILL.md:26-510` |
| Ralphinho DAG | RFC -> work units with deps+acceptance+tier; layers sequential, units parallel each in own worktree; stage depth by tier (trivial: impl->test ... large: +PRD-review+code-review+final-review); research=Sonnet, plan=Opus, implement=Codex, review=Opus; reviewer never wrote the code; merge queue rebases+tests, evicts on conflict/fail with context fed back; up to 3 passes | `autonomous-loops/SKILL.md:380-510` |
| GAN harness | Planner(sonnet) -> Generator(sonnet) <-> Evaluator(sonnet, Playwright MCP, live app); 5-15 iterations, pass at 7.0 weighted rubric; files `gan-harness/spec.md, eval-rubric.md, generator-state.md`; budget 50-200 USD; env vars raise models to opus | `skills/gan-style-harness/SKILL.md:36-148`; `agents/gan-evaluator.md:44-61` |
| Context handoff | Files not prompts: spec/rubric/state md, SHARED_TASK_NOTES.md, `research.contextFilePath -> plan -> implement -> reviews` | `autonomous-loops/SKILL.md:265,504-509` |
| Verification | `verification-loop` 6 phases (build, types, lint, tests+coverage, security grep, diff review) -> PASS/FAIL; `eval-harness` EDD with pass@k / pass^k, code/model/human graders; Santa method: 2 independent reviewers same rubric, both must pass, fix cycle with cap + human escalation | `skills/verification-loop`, `skills/eval-harness`, `skills/santa-method/SKILL.md:26-64` |
| Loop design | Machine-decidable goal, boundary conditions (anti-Goodhart), independent+deterministic judge, Build may not edit acceptance, 3 retries then human, front-load all clarification, 4-condition gate before building a loop at all | `skills/loop-design-check/SKILL.md:37-107` |
| Hooks | 27 hook entries: pre-bash dispatcher, gateguard fact-force (forces investigation before edit/destructive cmd), config-protection (blocks linter-config edits), governance-capture, MCP health, suggest-compact, PreCompact; Stop: format+typecheck, console.log check, cost-tracker, evaluate-session, desktop-notify; profiles minimal/standard/strict | `hooks/hooks.json` |
| Memory / learning | continuous-learning-v2: hooks observe tool use -> atomic "instincts" confidence 0.3-0.9, project-scoped, `/evolve` clusters to skills, `/promote` to global | `skills/continuous-learning-v2/SKILL.md:12-212` |
| Cost controls | cost-tracker Stop hook (records only); CostTracker budget_limit in sample code; loop-operator escalates on "cost drift"; strategic-compact suggests manual compaction; Continuous-Claude `--max-cost/--max-duration` (third-party tool) | `hooks.json`; `agents/loop-operator.md:39-45`; `autonomous-loops/SKILL.md:244-253` |
| Self-tuning | harness-optimizer: baseline `scripts/harness-audit.js`, snapshot, change config, rerun audit + `tests/run-all.js`, auto-restore on regression; never rewrites product code | `agents/harness-optimizer.md:20-60` |

## 3. Tekton setup (V, repo README + artifacts)

| Aspect | What | Source |
|---|---|---|
| Pattern | Written brief + deterministic gate. "Parallel read-only diagnosers, sequential implementers so edits did not collide, and an adversarial verifier" | Tekton README "How We Used Agents" (about lines 72-96) |
| Verifier | `scripts/verify.mjs` recomputes geometry from component coordinates, not from the spec claimed numbers: builder cannot grade itself | same |
| Evidence trail | about 20 preserved `artifacts/verifier-report.*.failed.json`, `orchestrate-log.jsonl`, `run-summary.json`, playback states; `scripts/orchestrate.mjs` | `gh api` tree listing |
| Anthropic account | "Independent verifier sub-agents graded each reconstruction in isolated context windows"; verification ran on Opus 4.8; self-correction reran placement checks, all 20 tests passed | https://claude.com/blog/meet-the-winners-of-our-claude-opus-4-8-build-day-hackathon |
| Staging | Smallest verifiable piece first, Nanchan stayed green as regression anchor, scale only after gate passes | README |
| Limits | Domain-specific (3D reconstruction); no model-per-agent table, no cost data; full Notre-Dame unfinished | README, Anthropic page |

## 4. Evidence: works vs marketing

| Claim | Status | Basis |
|---|---|---|
| ECC win at Forum Ventures hackathon | S/U for placing | Self-claim + secondary; forumvc page lists no winners |
| ECC "battle-tested across multiple production applications" | U | README:2139, no measurements |
| ECC popularity (304k stars) | V (stars), but stars are not efficacy | gh api |
| Any ECC benchmark of its own agents/skills improving success or cost | **Not found** (grep for benchmark/measured: only a third-party mgrep "~2x fewer tokens" claim, longform guide:139, S) | grep |
| ECC reported pain | V: #2834 hook overhead ~1.6 s per Bash call; #3765/#3802/#3756 Windows hooks fail with SyntaxError on every request; #2573 gateguard fires on read-only Bash; about 100 open issues | `gh api search/issues`, repo affaan-m/ECC |
| GAN harness (origin = Anthropic paper) | V for paper: solo 9 USD/20 min with broken core feature vs harness 200 USD/6 h working; Opus 4.6 DAW run 124.70 USD/3h50m; evaluator weak out of box, tuned from logs; Opus 4.6 dropped sprints, kept planner, moved evaluator to a single end pass | https://www.anthropic.com/engineering/harness-design-long-running-apps |
| ECC own GAN skill measured | U (adaptation of the paper, "ECC-community" origin, defaults all Sonnet) | skill frontmatter |
| Tekton gate works | V for artifacts (failed reports preserved, passing run); no cross-run benchmark | repo |

## 5. Worth stealing for "spec -> turnkey overnight; Opus orchestrates, Sonnet/Haiku execute"

| # | Piece | Why | Source |
|---|---|---|---|
| 1 | Verifier recomputes from artifacts, never trusts builder claims; keep failed reports as evidence | Strongest anti-self-grading mechanism with real artifacts behind it | Tekton README; `verify.mjs` |
| 2 | loop-design-check rules: machine-decidable done + boundary (tests may not be deleted/loosened), Build cannot edit acceptance, retry cap 3 then escalate, front-load all questions before launch | Fits unattended overnight runs; cheap to add as a lint on bob-pipeline.yaml | `loop-design-check/SKILL.md:44-107` |
| 3 | Work-unit schema `{id, deps, acceptance[], tier}` + tiered pipeline depth (trivial impl->test ... large + final-review) | Spends Opus review only where risk is; maps to slices | `autonomous-loops/SKILL.md:413-453` |
| 4 | Merge queue with eviction: rebase, run tests, on failure feed conflict/test output back to implementer | Only if parallel units; gives clean retry context | `autonomous-loops/SKILL.md:472-502` |
| 5 | Sprint contract (generator and evaluator agree on "done" and test method before coding) | Anthropic-measured lesson; same as approved Gherkin before Coder | harness-design article |
| 6 | Stop-hook gates (format+typecheck, cost-tracker) and config-protection (block edits to linter/test config) | Cheap deterministic guard against Goodharting | `hooks/hooks.json` |
| 7 | harness-optimizer: audit baseline -> one change -> rerun regression -> auto-revert | Template for bob-bench-driven self-tuning of the harness | `agents/harness-optimizer.md:20-60` |
| 8 | De-sloppify: separate cleanup pass instead of "do not do X" instructions | Matches Architect/Hardener stage | `autonomous-loops/SKILL.md:318-378` |
| 9 | Periodically remove one harness component and re-measure (assumptions go stale with newer models) | Keeps harness minimal when moving to Opus 4.8 | harness-design article |

Do not steal: 311-skill breadth, instincts auto-learning (no measured benefit, contamination risk, context bloat), 27 always-on hooks (1.6 s/Bash overhead, Windows breakage reported), Codex stages, GAN design-quality rubric (frontend-specific).

## 6. Tension with "single writer + one final verifier"

| Finding | Effect |
|---|---|
| Tekton: sequential implementers (one writer at a time) + adversarial verifier, plus parallel READ-ONLY diagnosers | Supports single writer; adds parallel read-only recon/diagnosis as free speedup |
| Anthropic paper: with Opus 4.6 per-sprint evaluator replaced by one end-of-run evaluator; planner kept | Supports one final verifier for strong models; evaluator pays off only at the edge of model capability |
| Anthropic paper: Sonnet 4.5 needed context resets (context anxiety); Opus 4.5 did not | Sonnet/Haiku executors may need resets/small slices; Opus-only evidence does not transfer |
| ECC Ralphinho: parallel writers in worktrees + merge queue; per-tier multi-review; Santa: TWO independent reviewers, both must pass | Contradicts: multiple writers and verifiers. Justified for parallel units or ship-to-production content; cost not measured (U) |
| ECC GAN: evaluator after every sprint, 5-15 iterations | Contradicts "one final": per-slice checks catch drift earlier; paper shows about 20x solo cost |
| loop-design-check: judge must differ from Build, be deterministic, final "done" flipped by a human | Even "one final verifier" must be independent and deterministic, not Build re-reading itself |

Gaps: no organiser primary for the Forum Ventures result; X posts blocked (HTTP 402); no ECC token/cost benchmarks found; Tekton `scripts/orchestrate.mjs` not read in detail; no model-per-agent info for Tekton; Opus 4.8 vs Fable 5 event naming unresolved.
