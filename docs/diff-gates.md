# diff_gates: gates on a work unit's diff (any stack)

The script `scripts/diff_gates.py` (stdlib). The core knows report formats, not stacks: the report is produced by the project's tool, and the script counts only over the diff lines from `--base` (working tree, including untracked files). It is invoked like an ordinary profile command: the exit code decides.

Exit codes: `0` passed, `1` failed, `3` cannot be evaluated (report missing or unparsed, not a git repository, unknown format, ambiguous path match). No report = failure; there is no silent skip.

`python scripts/diff_gates.py <gate> --base <sha> [--root .] [--config powerpuff.yaml] [--json]`. With `--json` it prints `{gate, ok, value, threshold, details:[...]}`; on exit code 3 it adds `error`.

## Gates
| Gate | What it counts | Input | Keys |
|---|---|---|---|
| `coverage` | coverage of added and changed executable lines (test files are not counted) | `--report` path or glob; cobertura, lcov, go coverprofile, coverage.py json, jacoco | `--format auto`, `--min 80`, `--min-lines 1`, `--missing-file fail\|ignore`, `--exclude` (repeatable) |
| `duplication` | new copy-paste: a window of `--min-lines` lines fully added in the diff that occurs elsewhere in tracked code (another file or another place in the same file) | git only | `--min-lines 6`, `--max-new 0`, `--exclude` |
| `complexity` | functions crossing the changed lines with cyclomatic complexity above the threshold | `--report`; lizard csv/xml, eslint json, radon json, gocyclo, sarif | `--format auto`, `--max 15`, `--base-report`, `--exclude` |
| `size` | unit budget: changed lines (added + deleted) and files, excluding tests, lock files and generated files | git only | `--max-lines 400`, `--max-files 25`, `--exclude` |
| `all` | runs the gates from the profile's `diff_gates` section | `--config` | `--out` a `quality-report.json` fragment |

## Rules
| Topic | Behavior |
|---|---|
| Line not in the report, file in the report | not executable, ignored |
| Changed code file not in the reports | all its added code lines count as uncovered and are listed; `--missing-file ignore` disables this; extensions outside the `gate_guard` language table are not counted |
| Zero changed executable lines (or fewer than `--min-lines`) | passed with a note |
| Report and repository paths | absolute, backslashes, `file://`, cobertura `sources`, module prefix from `go.mod`, paths relative to a subproject (suffix match); an ambiguity affecting a changed file gives exit code 3 with a list |
| Duplicates: what is not counted | windows of trivial lines (braces, imports, `end`); empty and comment lines (prefixes by extension, otherwise `#`, `//`); code moved from elsewhere (it is deleted there); windows overlapping within a file |
| Duplicates: what is indexed | files with a known code extension up to 1 MB; excluded: `node_modules`, `bin`, `obj`, `dist`, `build`, `target`, `vendor`, `.git`, generated and minified files |
| Complexity and base report | fail: complexity above `--max` and (the function is not in the base report or became more complex than on the base); complexity not above the base passes as `pre-existing`; without a base report every one fails |
| Complexity: no line range | gocyclo gives only the start line: the intersection is checked by it, and `details` carries a note |
| SARIF | rules whose id or name contains `complex`; the number is taken from the message text |

## Commands that produce the reports
| Stack | Coverage | Complexity |
|---|---|---|
| .NET | `dotnet test --collect:"XPlat Code Coverage"` (coverlet, cobertura in `TestResults/**/coverage.cobertura.xml`) | lizard |
| JS/TS | `c8 --reporter=lcov npm test` or `jest --coverage --coverageReporters=lcov` (`coverage/lcov.info`) | `eslint -f json -o eslint.json --rule '{"complexity":["warn",1]}' src` (threshold 1: all functions are in the report, the gate threshold is set by `--max`) |
| Python | `coverage run -m pytest && coverage json` (`coverage.json`) or `coverage xml` (cobertura) | `radon cc -j -s src > radon.json` |
| Rust | `cargo llvm-cov --lcov --output-path lcov.info` | `lizard -l rust --csv src > lizard.csv` |
| Go | `go test -coverprofile=cover.out ./...` | `gocyclo . > gocyclo.txt` |
| JVM | `mvn verify` with jacoco (`target/site/jacoco/jacoco.xml`) | `lizard -l java --csv src > lizard.csv` or `--xml` |
| Any language | cobertura or lcov, whichever the tool supports | `lizard --csv .` (format `lizard-csv`), `lizard --xml .` (format `lizard-xml`) |

## Profile
```yaml
gates:
  unit: [diff-coverage, duplication, complexity, size]
commands:
  diff-coverage: python scripts/diff_gates.py coverage --base {base} --report coverage/lcov.info --min 80
  duplication:   python scripts/diff_gates.py duplication --base {base} --max-new 0
  complexity:    python scripts/diff_gates.py complexity --base {base} --report eslint.json --max 15
  size:          python scripts/diff_gates.py size --base {base} --max-lines 400
diff_gates:              # for `diff_gates.py all --base {base} --config powerpuff.yaml --out diff-gates.json`
  coverage:   {report: coverage/lcov.info, min: 80, missing_file: fail}
  duplication: {min_lines: 6, max_new: 0}
  complexity:
    report: eslint.json
    max: 15
    base_report: base/eslint.json
  size:       {max_lines: 400, max_files: 25}
```
### Substitutions in commands
The driver script substitutes into the profile command strings (`commands`, `format_fix`, the queue's `final.commands`) before running, without expanding shell variables: `{base}` — the commit the unit started from, `{root}` — the repository root, `{plugin}` — the plugin root (forward slashes). An unknown `{...}` is left as is. The same values are exported to the gate's environment: `POWERPUFF_BASE`, `POWERPUFF_ROOT`, `POWERPUFF_PLUGIN`. Calling a plugin script in a profile: `python "{plugin}/scripts/diff_gates.py" size --base {base} --max-lines 400` (the profile is portable between machines).

| Where | `{base}` |
|---|---|
| `forge_run.py` | `start_sha` from `.forge/state.json` (HEAD at run start); in the clean-base check, the current HEAD (empty diff: diff gates pass by the "no changed lines" rule) |
| `queue_run.py`, final contour | the run's `base_sha` |
| `hook_stop.py` | `HEAD` (diff = uncommitted edits) |
| `prepare_run.py`, base check | the current HEAD |

Without PyYAML the `diff_gates` section is read by a built-in parser: two levels, lists only in the form `[a, b]`. `all` writes a fragment `{"schema": 1, "tool": "diff_gates", "gates": [...]}` with ids `diff-coverage`, `duplication`, `complexity`, `size`; a gate that could not be evaluated gets `status: failed` and an `error` field.

## Limitations
| What | Why |
|---|---|
| Coverage and complexity work on languages for which a report exists; the script itself does not build reports | the core does not know stacks |
| Duplicates are found by normalized lines, not syntax: renaming variables hides a copy | the price of simplicity; jscpd catches semantic clones |
| For `size`, a rename without edits is not counted; binary files are skipped | `git diff --numstat -M` |
| Test files are determined by the `gate_guard` language table (including the substring `test` in the path) | a single source of truth |
