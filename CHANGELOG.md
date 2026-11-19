# Changelog

## 0.2.0
- `forge_run.py --engine plain` (default): one Opus session (questions + frozen acceptance tests, then the code via `--resume`), gates from the profile run by the script, fix rounds in the same session (`--max-fix-rounds`, 4); `--accept` opt-in. See `docs/engine-plain.md`.
- No `powerpuff.yaml`: gates drafted from the stack presets into `.forge/powerpuff.draft.yaml` (not approved); gates red on the untouched base are excluded from a draft and listed in the report, an approved profile keeps a red base fatal.
- Fix rounds stop when a round leaves the tree unchanged.
- Spec `R<n>` ids not named by any acceptance test get one cover turn before the freeze; the rest is reported as `uncovered`.
- `diff_gates duplication` ignores clones inside frozen acceptance tests (still catches copies from them into code).
- Fix: resumed sessions were costed with the session's cumulative `total_cost_usd`; only the increment is counted now.

## 0.1.0
- Initial import from bob-pipeline 1.15.0: `scripts/forge_run.py` (script orchestrator), `scripts/forge_check.py` (deterministic done-check), `/forge` command, design and research docs.
