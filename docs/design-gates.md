# Quality gates layer: scripts decide, not the LLM (design, 2026-11-17)

Sources: `docs/research/quality-gates-evidence.md`, an analysis of the bob-pipeline gates and of the `KirillMaster.QualityGates` package.

## Rule
**The model proposes, the script decides.** No statement by the model ("tests are green", "done", "covered") affects the verdict. The verdict is the exit codes of commands that the driver itself ran.

| No. | Rule | Why |
|---|---|---|
| 1 | A gate is a command with an exit code; the driver runs it, not the model | in bob-pipeline the linter and test exit codes were taken from reports written by the roles |
| 2 | No tool or report — the gate fails, it is not skipped | in bob-pipeline a missing tool counted as passed |
| 3 | The set of gates and thresholds live in the repository and are approved by a human; the model writes only a draft on first run | currently the audit chooses the gates for each run — that is a hole |
| 4 | Gates count only new code (the diff); old violations go in a protected list | 89% of problems in agent PRs are complexity, duplicates, dead parameters; there is no point fixing all the old code in an autonomous run |
| 5 | Bypass protection is checked on the diff | agents fit code to the check and edit tests; more often on an ambiguous task |
| 6 | A single result format `quality-report.json` | one driver reads the gates of any stack |
| 7 | LLM checking is advisory: its findings become tests or cards, but not a verdict | judge models are unreliable |

## Bypass protection (script `gate_guard`, on the diff from the base commit)
| What it catches | How |
|---|---|
| Editing or deleting frozen tests | sha256 (already exists) |
| Skipping tests | added `Skip=`, `.skip(`, `xit(`, `@pytest.mark.skip`, `[Ignore]` |
| Suppressing warnings | added `#pragma warning disable`, `SuppressMessage`, `eslint-disable`, `@ts-ignore`, `# noqa`, `# type: ignore` |
| Weakening configs | changes to profile files, linter, analyzer and coverage configs, the old-violations list |
| Reducing the number of tests | test count after ≥ test count before |
Red on base: after the tests are frozen, the driver runs the test gates once (profile key `test_gates`, otherwise ids containing `test`) on the tree without the implementation; green tests prove nothing — one session adds failing tests, and if it is still green after that, the report marks the run "vacuous".
Exceptions only as a list in the profile that a human approves. Hardcoding of expected values is not caught by this; against it there is acceptance by a strong model and mutations on the diff.

## What runs when
| Level | Gates | Cost |
|---|---|---|
| Each writer iteration | build with warnings as errors, format, linter, types, fast tests, `gate_guard` | seconds–minutes |
| Unit of work (before "done") | acceptance tests, architecture rules, diff coverage, complexity and duplicates over changed files, public API diff, "the new test fails on a clean base" | minutes |
| End of the autonomous run (integration branch) | mutations on the diff, integration and contract tests, secret and vulnerability scanner, long run | tens of minutes |
No data was found that strict gates reduce task success; there are no time measurements either, so the levels will be refined on the first runs.

## Any stack: the core knows only the contract
The core knows no language. For it a gate is a line `identifier → command` in the project profile; the command returned 0 — passed. A project in Rust, Go, Java or anything else is plugged in with a single profile, with no changes to the plugin.

| Layer | What is in it | Stack binding |
|---|---|---|
| Core | running commands, exit codes, `quality-report.json`, levels (iteration / unit / end of run), bypass protection | none |
| Language table for bypass protection | signs of test files, test skipping, warning suppression, test declaration | data, not code; extended in the project profile |
| Ready-made profiles (presets) | typical commands for a stack | optional; the audit writes the first profile draft from the manifests |

Presets are only starting values, they can be changed:
| Stack | Iteration | Unit | End of run |
|---|---|---|---|
| .NET | `dotnet build -warnaserror`, `dotnet format --verify-no-changes`, `dotnet test` with the `KirillMaster.QualityGates.Build` package | `qg pr` (new violations, diff coverage, duplicates, architecture, complexity) | `qg mutation-diff` |
| TypeScript | `tsc --noEmit`, ESLint, Prettier, fast tests | dependency-cruiser, diff coverage | Stryker on the diff |
| Python | ruff, pyright, pytest | import-linter, diff-cover | mutmut on the diff |
| Rust | `cargo fmt --check`, `cargo clippy -- -D warnings`, `cargo test` | `cargo llvm-cov` + diff-cover, `cargo deny` | `cargo mutants --in-diff` |
| Go | `gofmt -l`, `go vet`, golangci-lint, `go test` | diff coverage, `go-arch-lint` | mutations on the diff |
| Any | — | lizard (complexity), jscpd (duplicates), diff-cover, secret scanner | — |
The Rust and Go presets were written from the tools' documentation and have not yet been verified on real runs.

If a language has no entry in the table, bypass protection still guards the protected files and frozen tests, and the report says that it is blind for that language.

Result format (matches the `qg` contract):
```json
{"schema": 1, "tool": "<package>", "gates": [{"id": "tests", "status": "passed|failed|skipped", "blocking": true, "value": 74.2, "threshold": 80}]}
```
A green report is one with no gate that has `blocking: true` and `failed`. For powerpuff additionally: a required gate with status `skipped` counts as failed.

## Project profile (`powerpuff.yaml`, in the repository)
```yaml
stack: dotnet            # dotnet | typescript | python | mixed
gates:
  iteration: [build, format, lint, tests-fast, guard]
  unit:      [acceptance, architecture, diff-coverage, complexity, duplication, api-diff]
  final: [mutation-diff, integration, secrets]
commands:                # a command for each identifier; for .NET the default is `qg run --gate <id>`
  build: dotnet build -warnaserror
protected: [powerpuff.yaml, .editorconfig, Directory.Build.props, quality/**]
allow_suppressions: []   # exceptions for gate_guard
```
The audit writes the first draft from the manifests; until a human approves it, the run report is marked "gates not approved".

## Attended mode
The same scripts are hooked into Claude Code: the Stop hook runs the iteration-level gates and with exit code 2 prevents ending the turn; PreToolUse blocks edits to protected files. This way the rule applies even without the autonomous-mode driver.

## Order of work
1. `gate_guard` and running the gates from `powerpuff.yaml` in `forge_check` (replacing `.forge/gates.json`).
2. A `quality-report.json` adapter for .NET via `qg`; then TypeScript and Python.
3. Hooks for attended mode.
4. Each gate stays only if on the bench or in a pilot it caught a defect that would otherwise have passed.

## Unit gates on the diff: `diff_gates`
Diff coverage, duplicates, complexity and unit size — the script `scripts/diff_gates.py`, profile commands with an exit code (0/1/3), like `gate_guard`. The core knows report formats (cobertura, lcov, go, coverage.py, jacoco; lizard, eslint, radon, gocyclo, sarif), not stacks; no report — code 3, i.e. failure. Details and commands per stack: `docs/diff-gates.md`.
