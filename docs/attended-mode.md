# Attended mode: Claude Code hooks

Two plugin hooks for sessions with a human. Both are opt-in: without `powerpuff.yaml` or without a `hooks:` section they are a silent no-op (exit 0).

| Hook | Event | What it does |
|---|---|---|
| `scripts/hook_stop.py` | Stop | Prevents ending the turn while `iteration`-tier gates are red |
| `scripts/hook_protect.py` | PreToolUse (`Edit\|Write\|MultiEdit\|NotebookEdit`) | Blocks edits to protected files |

Registration: `hooks/hooks.json`. Project root = the nearest ancestor of `cwd` containing `powerpuff.yaml` (the search stops at the git root).

## Enabling

```yaml
hooks:
  stop_gates: true
  protect: true
  stop_max_blocks: 3
  stop_timeout_s: 300
```

| Key | Default | Meaning |
|---|---|---|
| `stop_gates` | false | Enables the Stop hook |
| `protect` | false | Enables the protect hook |
| `stop_max_blocks` | 3 | Consecutive blocks per session before release |
| `stop_timeout_s` | 300 | Timeout for each gate and `format_fix` |

## Stop

| Situation | Result |
|---|---|
| Tree is clean (no changes vs HEAD, no untracked files) | exit 0, gates are not run |
| Tree matches the last green one (HEAD + status + mtime) | exit 0, cache |
| Gates green | exit 0, counter reset |
| Red gate or missing tool | exit 2: names of failed gates + output tail (up to 40 lines, up to 6000 characters total), ending with "Fix the cause; do not disable or skip the check." |
| `POWERPUFF_DRIVER=1` | exit 0 (the driver decides) |

Order: `format_fix` (if set), then the `iteration` gates via `forge_check.check_gates`. A missing tool = a red gate.

Loop guard: a counter of consecutive blocks per `session_id` in `.powerpuff/hook-state.json`. After `stop_max_blocks` the hook releases the turn (exit 0) and writes `gates still red after N attempts — stopping for the human` to stderr; the counter resets on green and after release.

## Protect

| Edit target | Result |
|---|---|
| Matches a glob from `protected` (same matching as in `gate_guard`) | exit 2: file, glob, "ask the human to change it; do not work around it" |
| `powerpuff.yaml` itself | exit 2 |
| Anything else, paths outside the project root | exit 0 |

The path is normalized relative to the root, with forward slashes; on Windows case is ignored. It also applies inside driver sessions.

## Relation to the autonomous driver

| Aspect | Driver (`forge_run`, `prepare_run`) | Attended |
|---|---|---|
| Who decides on gates | Script | Stop hook |
| Stop hook | Disabled (`POWERPUFF_DRIVER=1` in the `claude` environment) | Active |
| Protect hook | Active | Active |
| Queue `--attended` | Pause after each unit (see `docs/queue-format.md`) | Unrelated to the hooks |

## Deliberate bypass

| Method | Action |
|---|---|
| One-off edit of a protected file | The human starts the session with `POWERPUFF_ALLOW_PROTECTED=1` |
| Turn a hook off | `stop_gates: false` / `protect: false` in `powerpuff.yaml` (edited by the human) |
| Reset cache and counters | Delete `.powerpuff/hook-state.json` |

The model must not bypass the hooks: not by renaming a file, not by writing through the shell, not by disabling a check.

Internal hook error: exit 0 and a `powerpuff hook error: ...` line on stderr; the session does not crash.
