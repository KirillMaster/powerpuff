# Project profile `powerpuff.yaml`, presets, `quality-report.json`

The profile lives in the project repository and is approved by a human. The core knows only the contract "gate identifier → command → exit code"; the stack is set by the profile. Design: `docs/design-gates.md`, diff gates: `docs/diff-gates.md`.

## Profile keys
| Key | Type | Meaning | Read by |
|---|---|---|---|
| `approved` | bool | a human reviewed and approved the profile; `false` = the report is marked "gates not approved" | `forge_run`, `queue_run`, `quality_report` |
| `stack` | string | informational (`dotnet`, `mixed`, ...) | nobody branches on it |
| `gates` | map tier → list of ids | `iteration` (seconds–minutes, on every iteration), `unit` (before "done"), `final` (end of run, heavy) | `forge_run` (iteration + unit = the "done" check, `final` via `--final`), `queue_run` (`final`), `hook_stop` (`iteration`), `quality_report` (any) |
| `commands` | map id → command | the gate's command, exit code 0 = passed; missing tool = failure | the same |
| `format_fix` | string | formatter in fix mode; the writer runs it before the gates | `forge_run` |
| `setup` | string | dependency installation (`npm install`, `pip install -r requirements.txt`, `dotnet restore`); once, before the base check | `forge_run` |
| `protected` | list of globs | protected files (linters, analyzers, CI): editing = violation; added to the default list | `gate_guard`, `hook_protect` |
| `unprotected` | list of globs | remove a glob from the default list | `gate_guard` |
| `allow_suppressions` | list of `{rule, file, pattern}` | permitted warning suppressions | `gate_guard` |
| `languages` | map | own row of the language table (ext, test_file, skip, suppression, test_decl); without PyYAML only in `powerpuff.gates.json` | `gate_guard` |
| `test_gates` | list of ids | which gates check "red on base"; without the key — ids containing `test` | `forge_run` |
| `test_infra` | list of globs | manifests the test writer may change | `forge_run` |
| `advisor` | string | Claude Code advisor model (`fable` / `opus` / `sonnet` / full id; `off`) for plain build and fix sessions, passed as `--settings` `advisorModel`; pair it with a cheaper `model_build` / `model_fix`. Without the key: user env `POWERPUFF_ADVISOR` | `forge_run` |
| `diff_gates` | map | thresholds for `diff_gates.py all` | `diff_gates` |
| `hooks` | — | not a key: the plugin hooks (`hooks/hooks.json`) take `gates.iteration` and `protected` from the profile | `hook_stop`, `hook_protect` |

Without PyYAML the profile is read by the built-in parser `gate_guard.parse_simple_yaml`: flat keys, `[a, b]` lists, single-level maps. Write commands in the profile in single quotes.

## Substitutions in commands
| What | Substituted by | Value |
|---|---|---|
| `{base}` | the driver when running a gate | the commit the unit started from (`--base` of `quality_report`, default HEAD = empty diff) |
| `{root}` | driver | repository root, forward slashes |
| `{plugin}` | driver | plugin root (the directory with `scripts/`), forward slashes; stays as is in the profile |
| `{pm}`, `{pmx}` | only in the TypeScript preset; `profile_init` | `npm run` / `pnpm run` / `yarn run` and `npx --no-install` / `pnpm exec` / `yarn`, by lock file |

The same values are exported to the gate's environment: `POWERPUFF_BASE`, `POWERPUFF_ROOT`, `POWERPUFF_PLUGIN`. The profile is portable: `profile_init` keeps `{plugin}` and it can be committed; `--plugin DIR` substitutes the path explicitly (the profile stops being portable).

## Presets (`presets/<stack>.yaml`)
A preset is the profile's starting values plus service keys: `detect` (stack markers), `needs` (the condition under which a gate enters the profile), `global` (gates for the whole repository, once).

| Stack | Markers (`detect`) | Required tools | Others (the gate enters the profile only if present) |
|---|---|---|---|
| dotnet | `*.sln`, `*.slnx`, `*.csproj` | `dotnet` SDK | coverlet.collector (coverage), `lizard`, `qg` (`KirillMaster.QualityGates.Cli`; `qg pr`, `qg mutation-diff`, codes 0/1/2/3 — non-zero = failure) |
| typescript | `tsconfig.json`, `package.json` with `typescript` | `node`, package manager | typescript, eslint, prettier, vitest / jest / c8, dependency-cruiser, Stryker, jscpd, `lizard` |
| python | `pyproject.toml`, `setup.py`, `setup.cfg`, `requirements*.txt`, `Pipfile` | `python` | pytest, ruff, pyright or mypy, coverage, import-linter, mutmut, `lizard` |
| rust | `Cargo.toml` | `cargo` | rustfmt, clippy, cargo-llvm-cov, cargo-deny, cargo-mutants, `lizard` |
| go | `go.mod` | `go`, `gofmt` | golangci-lint, go-arch-lint, gremlins, `lizard` |

Rust and Go are written from the tools' documentation and have not been verified on real repositories.
Everywhere there are gates that need no external tools: `duplication`, `size` (`diff_gates.py`, needs only git), and Python also has `syntax` (`compileall`).
By default tests run once (`tests`) and a second time with coverage (`coverage`): the price is a double run.

### `needs` conditions
The value is terms joined with `+` (all required); a term is `kind:variant|variant` (one is enough). A variant can change the kind: `tool:ruff|has:pyproject.toml~tool.ruff`.
| Kind | Satisfied if |
|---|---|
| `tool` | an executable is in PATH (or in the project's `node_modules/.bin`) |
| `file` | the project (depth up to 6, excluding `node_modules`, `bin`, `obj`, `target`, `vendor`, `.venv`) has a file matching the glob |
| `has` | `glob~text`: a file matching the glob contains the text |
| `dep` | the package is declared in `package.json` or is in `node_modules/.bin` |
| `script` | `package.json` has the script (the `no test specified` stub does not count) |
| `tests` | there is a test file matching the glob |
| `gate` / `no-gate` | another gate of this project is kept / not kept (alternatives: an eslint script or a direct call) |

## `scripts/profile_init.py`: a profile draft without a model
`python scripts/profile_init.py [--repo .] [--write] [--stack name,...] [--plugin DIR]`
| Behavior | How |
|---|---|
| Stack detection | `detect` at the root and in subdirectories up to depth 2; several stacks or directories = monorepo |
| Monorepo | prefixed ids (`web-lint`, `dotnet-build`); subdirectory commands are `cd <dir> && ...`; `diff_gates` gets `--root {root}`, coverage gets `--missing-file ignore`; `duplication` and `size` once per repository |
| Dropping | gates whose tool or configuration exists in the repository are kept; the rest become a "recommended to add" comment with the reason |
| Output | draft to stdout, `approved: false`, header "a human must review and set `approved: true`" |
| `--write` | writes `powerpuff.yaml` only if neither `powerpuff.yaml` nor `powerpuff.gates.json` exists; never overwrites |
| Stack not found | a stub with `commands: {}`, message "no preset — write the profile by hand", code 2 |

Exit codes: `0` draft produced; `1` `--write` refused (profile already exists); `2` no preset.
Limitations: on a monorepo with several stacks check prefixes and paths by hand; a tool being "installed" = present in PATH on the machine where generation runs.

## `scripts/quality_report.py`: tier gates and `quality-report.json`
`python scripts/quality_report.py --repo . [--tier iteration|unit|final|all] [--base <sha>] [--out quality-report.json] [--timeout 1800]`
The profile is read by the loader `forge_run.load_profile`; each gate is run separately through `forge_check.check_gates` with the `{base}`/`{root}`/`{plugin}` context; `exit_code` and `seconds` come from the `check_gates` result.
At the end of a run (`done` or `failed`) `forge_run` writes `.forge/quality-report.json` with the same schema (the last result of each gate, plus `guard`) and a link with a table in `REPORT.md`; there is one builder: `forge_check.quality_report`.
Exit codes: `0` all green; `1` there is a red gate; `3` no profile, the profile is invalid, the tier has no gates or a tool is not found (takes precedence over `1`).

### `quality-report.json` schema, version 1
| Field | Type | Meaning |
|---|---|---|
| `schema` | int | 1 |
| `tool` | string | `powerpuff.quality_report`; for the `forge_run` report — `powerpuff.forge_run` (tier `all`, plus the `guard` field) |
| `generated_at` | string | UTC, ISO 8601 |
| `head` | string or null | repository HEAD sha |
| `base` | string or null | the `{base}` commit |
| `profile_approved` | bool | the profile's `approved` |
| `tier` | string | `iteration`, `unit`, `final` or `all` |
| `gates[]` | array | in profile order: `name`, `tier`, `ok`, `exit_code`, `seconds`, `missing_tool`, `output_tail` |
| `ok` | bool | all gates are green |
`exit_code`: the real return code, `null` when the tool is missing or on timeout; `seconds` — the `check_gates` measurement (0.1 s). `output_tail` — the last 2000 characters of a red gate's output (for `forge_run` — 600). The format is minimal and does not match the `qg` report (`schema`, `tool`, `gates[{id, status, blocking, value, threshold}]`): the `status`/`blocking` fields are not needed while all profile gates are mandatory.
The files `quality-report.json`, `powerpuff.yaml` and `.powerpuff-*` (lizard reports, diff for mutations) stay in the working tree as untracked: add them to `.gitignore`; the `size` gate excludes them.

## Stack without a preset
1. `python scripts/profile_init.py --repo .` — code 2 and a stub; `--write` saves it as `powerpuff.yaml`.
2. Fill in `gates` and `commands`: one gate per check, a command with an exit code, run from the root; stack-independent gates (`diff_gates.py duplication|size|complexity`) are taken as is.
3. For `complexity` and `coverage` the report is produced by the stack's tool (`docs/diff-gates.md`, table "Commands that produce the reports"); lizard knows more than 20 languages.
4. List the linter, build and CI configs in `protected`.
5. Run `quality_report.py --tier iteration`, then `--tier unit`; fix; set `approved: true`.
6. For a language outside the table, `gate_guard`'s bypass protection is blind to skipped tests and suppressions; its `languages` is extended in `powerpuff.gates.json`.

Example (repository C: Kotlin + Gradle, an Android app subproject; no preset). The keys `format_fix` and `test_gates` are not set: the project has no formatter and no tests.
```yaml
approved: false
stack: kotlin-gradle
gates:
  iteration: [gradle-help]
  unit: [duplication, size, complexity]
  final: [assemble]
commands:
  gradle-help: '.\gradlew.bat --no-daemon -q help'
  duplication: 'python "{plugin}/scripts/diff_gates.py" duplication --base {base} --max-new 0'
  size: 'python "{plugin}/scripts/diff_gates.py" size --base {base} --max-lines 400 --exclude "quality-report*.json" --exclude powerpuff.yaml --exclude ".powerpuff-*"'
  complexity: 'lizard -l kotlin --csv -x "*/build/*" . > "{root}/.powerpuff-lizard.csv" && python "{plugin}/scripts/diff_gates.py" complexity --base {base} --report "{root}/.powerpuff-lizard.csv" --max 15'
  assemble: '.\gradlew.bat --no-daemon assembleDebug'
protected: ['build.gradle.kts', '*/build.gradle.kts', 'settings.gradle.kts', 'gradle.properties', 'gradle/**', '.editorconfig', '.github/workflows/**']
```
`{plugin}` is expanded by the driver. On Windows `cmd.exe` does not look for a program in the current directory, hence `.\gradlew.bat` rather than `gradlew.bat`; on POSIX — `./gradlew`.

## Verification on real repositories (criterion 2.4)
Three local repositories of three stacks; work only in local clones (`git clone --local --no-hardlinks`), the clones were deleted. Gates without a model, `{base}` = HEAD (empty diff), `--timeout 900`.

| Repo | Stack | Tier | Gate: result, seconds |
|---|---|---|---|
| A | .NET (sln, web application, 1 test project) | iteration | build: pass 28.1; format: pass 39.0 |
| A | | unit | tests: pass 12.6; coverage: pass 6.5; diff-coverage: pass 0.4; duplication: pass 0.4; size: pass 0.4; complexity: pass 0.6 |
| B | Python (pyproject, pytest) | iteration | syntax: pass 0.1 |
| B | | unit | tests: pass 5.8; coverage: pass 6.6; diff-coverage: pass 0.6; duplication: pass 0.5; size: pass 0.5; complexity: pass 0.8 |
| C | Kotlin + Gradle, no preset (profile by hand) | iteration | gradle-help: pass 67.6 |
| C | | unit | duplication: pass 0.6; size: pass 0.6; complexity: pass 1.3 |

Not run: the `final` tier (A: `qg` not installed; B: `mutmut` is slow; C: `assemble` needs the Android SDK), `diff-coverage` on a non-empty diff. An empty diff yields "passed: no changed lines" — the diff gates here were verified for launching and report parsing, not for catching violations (that is done by `tests/test_diff_gates.py`).

What had to be fixed after the first run:
| Repo | What happened | Fix |
|---|---|---|
| A | the generated `size` is red (796 lines): an untracked coverage report in `TestResults/` and the plugin's own files were counted as changed code | `--exclude` for `quality-report*.json`, `powerpuff.yaml`, `.powerpuff-*` and the stack's coverage artifacts was added to the `size` command of all presets |
| B | the `iteration` tier is empty: the repository has no ruff / pyright / mypy configuration | a `syntax` gate (`python -m compileall`) was added to the Python preset; the rest goes to "recommended to add" |
| C | `profile_init` on a subdirectory: code 2, a stub (as designed) | the profile was written by hand (example above) |
| C | `gradlew.bat`: "not recognized" (code 9009, `missing_tool`) | `.\gradlew.bat` |
| C | the lizard report in `{root}/.git/...`: there is no `.git` directory in a monorepo subproject | the report goes to `{root}/.powerpuff-lizard.csv`; all presets were fixed the same way |
At the root of monorepo C `profile_init` finds Python and .NET and does not see Kotlin and shell: subprojects without a preset have to be handled by hand.

Gaps: `forge_run.py` does not write `quality-report.json` itself (the report is made by `quality_report.py`); a red gate's `exit_code` is not saved; Rust and Go are not verified.
