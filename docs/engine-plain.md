# The `plain` engine — one model context + script-run checks

The measurement showed that a single Opus session with no orchestration gives the same quality as the multi-session `forge` at a cost 3–4 times lower (8 times on the multi-stage project). So `forge_run.py --engine plain` is the default: one model, one context (`--resume <session_id>`), and everything that can be checked deterministically is done by the script and spends no sessions. `--engine forge` is the previous pipeline, unchanged. The decision is recorded in `.forge/state.json` (`engine`), and a restart preserves it.

## Run flow
| Step (`state.stage`) | Who | What happens |
|---|---|---|
| setup | script | the profile's `setup` command (dependency install: TS presets — `{pm} install`, Python — `pip install -r requirements.txt` if the file exists, .NET — `dotnet restore`) once before the base check; on failure: approved profile → failed, draft → a warning in the report |
| base check | script | all profile gates on the untouched base (skipped in the queue if this tree was already green; setup runs anyway). Approved profile: red base → failed (`--allow-red-base` skips). Draft from presets: gates red on the base are excluded and listed in the report |
| `plain-tests`, turn 1 | model (`--model`, default the profile's `model`, otherwise opus) | questions `Q<n> [blocking\|non-blocking\|info]: … \| default: … \| cost of a wrong guess: …` → `.forge/questions.json`; acceptance tests only as NEW files; writes no code |
| test guard | script | edits to existing files are reverted (exceptions: manifests, `test_infra`), foreign non-test files are deleted; no tests → one `--resume` with a request, then failed |
| questions | script | `blocking` unanswered and no `--assume-defaults` → `FORGE: failed (blocking questions: …)`, exit 1, resume from the same step; answers are returned to the model in the same session |
| requirement coverage | script | ids of the form `R<n>` from the spec not named in any new test → one `--resume` asking to add tests; what remains is the `uncovered` line in the report (does not block: after the freeze it can no longer be fixed) |
| freeze | script | `.forge/frozen.sha256` + `.forge/frozen.json`, commit; `hook_protect` blocks edits to these files in the run's sessions |
| red-on-base | script | test gates on the base must be red, otherwise the same session gets a `vacuous` request |
| `plain-build`, turn 2 | `--model-build` (otherwise the profile's `model_build`, otherwise `--model`), NEW session | "implement the spec, frozen tests: …, do not edit"; a spec ≤ 30K characters is inlined in the prompt, otherwise the path `.forge/spec.md`. The context of turn 1 (tests, questions) is not carried over |
| gates | script | `forge_check` + `gate_guard` by the profile's tiers; no tool = failure; an edit to a frozen test → restore from git + a violation (`frozen_violations`) |
| fix rounds | `--model-fix` (otherwise the profile's `model_fix`, otherwise the build model) | red → the build session continues (`--resume`) with the failed gates and a tail; another model → a new session with a short handoff (spec by path, `git diff --stat`, gates, red output). The last round with a model ≠ `--model` escalates to `--model`. No more than `--max-fix-rounds` (4). A round with no changes in the tree → `FORGE: failed (stuck: …)`, exit 1, resume from `plain-build`: the human answers via `.forge/disputes.md` (the decision is `answer` in `.forge/questions.json`, it goes into the next round) |
| accept | `--model`, fresh context | only with `--accept`: the previous Opus acceptance |
| report | script | `.forge/REPORT.md`, `quality-report.json`, the last line `FORGE: done\|failed` |

## Flags
| Flag | Meaning |
|---|---|
| `--engine plain\|forge` | engine, default `plain` |
| `--max-fix-rounds N` | how many times to continue the session with red gates (4) |
| `--model-build` / `--model-fix` | models for the implementation turn and fix rounds (profile: `model_build` / `model_fix`) |
| `--advisor M` | advisor model of build/fix sessions (profile: `advisor`; `off` disables). The session gets `--settings` with `advisorModel` and a prompt line to consult it; calls are counted in `state.json` `advisor`, an unavailable advisor is a WARNING in `progress.md`, the run continues |
| `--accept` | one independent acceptance after green gates |
| others | as in forge: `--model`, `--max-usd`, `--max-hours`, `--no-wait`, `--max-outage-min`, `--assume-defaults`, `--allow-red-base`, `--final`, `--stream`; `--preflight` is not supported (no audit) |

In `queue.yaml`: the key `engine: plain|forge`; the CLI `queue_run.py --engine` overrides it; the value is passed to each unit's `forge_run.py`.

## Requirements and limitations
| What | How |
|---|---|
| profile | gates from `powerpuff.yaml` (the model does not choose them). Without it — a draft from the stack presets in `.forge/powerpuff.draft.yaml` (`approved: false`); stack not recognized → `FORGE: failed`, no sessions spent |
| duplicates | `diff_gates duplication` does not count clones inside frozen tests (they cannot be edited), but catches copy-paste from them into code |
| `--model` | CLI option > `model` in the profile > `opus` |
| hook protection | `.forge/frozen.json` is honored by `hook_protect.py` regardless of `hooks.protect`; the file is deleted at the end of the run |
| API unavailable / limit | as in forge: waiting without spending a round, `FORGE: paused (API unreachable)`, exit 3; a restart continues |

## Resume
| Killed during | What a re-run does |
|---|---|
| turn 1 | repeats turn 1 with a fresh session (a snapshot of the dirty tree before the turn is kept in state) |
| turn 2 | continues the turn-1 session (`--resume`) with a note "previous attempt interrupted" |
| fix round | re-checks the gates and repeats that round in the same session |
