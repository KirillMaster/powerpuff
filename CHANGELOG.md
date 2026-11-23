# Changelog

## 0.3.1
English translation of docs, prompts and messages.

## 0.3.0
Cost/speed refactor after a 12-hour overnight run (64 units, 81 runs, ~$544 notional; see docs/HANDOFF-2026-11-22-cost-refactor.md).
- Unit prompts carry a slice of the shared spec (`scripts/spec_slice.py`) instead of the whole spec: yamlkit items named by the unit's `requirements`/text, their tasks, contracts and entities; markdown paragraphs with those ids; paths for the rest. 211K -> ~14K characters per unit on the reference project; the coverage check now asks only for the unit's own ids.
- Rate limits: a `rate_limit_event` with `status: rejected` (the CLI's "You've hit your 5-hour limit") is a rate limit, not an API outage: the run waits for `resetsAt` and continues the same stage instead of `FORGE: paused` and a restart from scratch. Sessions write the account's rate state to `POWERPUFF_RATE_FILE`.
- plain engine: the build turn is a fresh session (`--model-build` / profile `model_build`); fix rounds continue it, or start a fresh session with a short handoff when `--model-fix` / `model_fix` differs; the last fix round escalates to `--model`.
- A fix round that changes nothing ends as `FORGE: failed (stuck: ...)`, resumable at `plain-build`; the queue reports it as `needs-answer` with the start of `.forge/disputes.md`, no retry.
- queue_run: `--parallel N` / `parallel` (independent units at once in one integration branch; slots shrink at >=75%/90% of the 5-hour window and stop on a rejected window), `max_attempts` (default 2; the retry starts from a fresh branch with the previous reason and disputes), `already_merged`, `budget.unit_usd`, `model_build` / `model_fix`.
- queue_run: a tree whose gates were green once (base of a unit or the result of a done unit) skips the base check of later units (`--skip-base-check` still runs `setup`).
- Headless sessions run with `--strict-mcp-config` (no user MCP servers).

## 0.2.0
- `forge_run.py --engine plain` (default): one Opus session (questions + frozen acceptance tests, then the code via `--resume`), gates from the profile run by the script, fix rounds in the same session (`--max-fix-rounds`, 4); `--accept` opt-in. See `docs/engine-plain.md`.
- No `powerpuff.yaml`: gates drafted from the stack presets into `.forge/powerpuff.draft.yaml` (not approved); gates red on the untouched base are excluded from a draft and listed in the report, an approved profile keeps a red base fatal.
- Fix rounds stop when a round leaves the tree unchanged.
- Profile `setup` (dependency install) runs once before the base check; presets fill it for TypeScript, Python (requirements.txt) and .NET. A draft gate whose tool is missing is excluded like a red-on-base gate.
- Spec `R<n>` ids and yamlkit ids (`AS-n`, `AC-n`, `FR-n`) not named by any acceptance test get one cover turn before the freeze; the rest is reported as `uncovered`.
- `diff_gates duplication` ignores clones inside frozen acceptance tests (still catches copies from them into code).
- Fix: resumed sessions were costed with the session's cumulative `total_cost_usd`; only the increment is counted now.

## 0.1.0
- Initial import from bob-pipeline 1.15.0: `scripts/forge_run.py` (script orchestrator), `scripts/forge_check.py` (deterministic done-check), `/forge` command, design and research docs.
