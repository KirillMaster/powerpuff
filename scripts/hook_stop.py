"""Stop hook (attended mode): does not let the turn end while the profile's `iteration` gates are red.

Silent no-op unless the project has `powerpuff.yaml` with `hooks: {stop_gates: true}`, and always under the driver
(POWERPUFF_DRIVER=1: forge_run / prepare_run decide there). Exit 0 = allow, 2 = block (stderr goes to the model).
Loop guard: after `stop_max_blocks` consecutive blocks in one session the hook lets the turn end.
State: <project>/.powerpuff/hook-state.json. See docs/attended-mode.md.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import hook_protect

STATE_DIR = ".powerpuff"
STATE_FILE = "hook-state.json"
MAX_CHARS = 6000
TAIL_LINES = 40
TAIL_CHARS = 4000
KEEP_SESSIONS = 20


def git(root: Path, *args: str) -> str:
    p = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=root, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.stdout if p.returncode == 0 else ""


def changed_paths(root: Path) -> list[str]:
    """`git status --porcelain` lines (untracked files listed one by one), without our own state dir."""
    lines = git(root, "status", "--porcelain", "-uall").splitlines()
    return [ln for ln in lines if not ln[3:].startswith(STATE_DIR + "/")]


def tree_hash(root: Path, lines: list[str]) -> str:
    h = hashlib.sha256(git(root, "rev-parse", "HEAD").encode())
    for ln in sorted(lines):
        path = ln[3:].rsplit(" -> ", 1)[-1].strip('"')
        try:
            mtime = (root / path).stat().st_mtime_ns
        except OSError:
            mtime = 0
        h.update(f"{ln}\0{mtime}\0".encode())
    return h.hexdigest()


def load_state(root: Path) -> dict:
    try:
        data = json.loads((root / STATE_DIR / STATE_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(root: Path, state: dict) -> None:
    sessions = state.get("blocks") or {}
    state["blocks"] = dict(list(sessions.items())[-KEEP_SESSIONS:])
    (root / STATE_DIR).mkdir(exist_ok=True)
    (root / STATE_DIR / STATE_FILE).write_text(json.dumps(state, indent=1), encoding="utf-8")


def number(section: dict, key: str, default: int) -> int:
    try:
        return max(int(section.get(key, default)), 1)
    except (TypeError, ValueError):
        return default


def failure_report(results: dict) -> str:
    parts = []
    for name, r in results.items():
        if r["ok"]:
            continue
        tail = "\n".join((r.get("tail") or "").splitlines()[-TAIL_LINES:])
        head = f"gate `{name}` failed" + (" (tool missing: install it or ask the human; do not skip the gate)"
                                          if r.get("missing_tool") else "")
        parts.append(f"{head}:\n{tail}".rstrip())
    body = "\n\n".join(parts)
    footer = "\n\nFix the cause; do not disable or skip the check."
    room = MAX_CHARS - len(footer) - 40
    if len(body) > room:
        body = "...(truncated)\n" + body[-room:]
    return "powerpuff: iteration gates are red, the turn cannot end yet.\n" + body + footer


def run() -> int:
    if os.environ.get("POWERPUFF_DRIVER") == "1":
        return 0
    event = json.loads(sys.stdin.read() or "{}")
    if not isinstance(event, dict):
        return 0
    root = hook_protect.find_root(Path(event.get("cwd") or ".").resolve())
    if root is None:
        return 0
    section = hook_protect.hook_switches(root)
    if not hook_protect.switch_on(section, "stop_gates"):
        return 0
    lines = changed_paths(root)
    if not lines:
        return 0
    state = load_state(root)
    if state.get("green_hash") == tree_hash(root, lines):
        return 0

    import forge_check
    import forge_run
    prof = forge_run.load_profile(root)
    iteration = (prof["config"].get("gates") or {}).get("iteration") or []
    gates = {i: prof["gates"][i] for i in iteration if i in prof["gates"]}
    if not gates:
        return 0
    timeout = number(section, "stop_timeout_s", 300)
    ctx = forge_check.gate_context(root, "HEAD")
    if prof["format_fix"]:
        try:
            subprocess.run(forge_check.expand(prof["format_fix"], ctx), shell=True, cwd=root, capture_output=True,
                           timeout=timeout, env=forge_check.context_env(ctx))
        except (subprocess.TimeoutExpired, OSError):
            pass
    report = forge_check.check_gates(root, timeout, gates, ctx, TAIL_CHARS)

    sid = str(event.get("session_id") or "")
    blocks = state.setdefault("blocks", {})
    if report["ok"]:
        blocks.pop(sid, None)
        state["green_hash"] = tree_hash(root, changed_paths(root))
        save_state(root, state)
        return 0
    limit = number(section, "stop_max_blocks", 3)
    count = blocks.pop(sid, 0)
    if count >= limit:
        save_state(root, state)
        print(f"powerpuff: gates still red after {limit} attempts — stopping for the human", file=sys.stderr)
        return 0
    blocks[sid] = count + 1
    save_state(root, state)
    print(failure_report(report["results"]), file=sys.stderr)
    return 2


def main() -> int:
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        return run()
    except Exception as e:  # noqa: BLE001 - a hook must never crash the session
        print(f"powerpuff hook error: {e}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
