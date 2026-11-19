"""PreToolUse hook (attended mode): blocks Edit/Write/MultiEdit/NotebookEdit of the profile's protected files.

Silent no-op unless the project has `powerpuff.yaml` with `hooks: {protect: true}` or a run froze acceptance tests
(`.forge/frozen.json`, written by forge_run's plain engine: those files are protected for the rest of the run). Exit 0 = allow, 2 = block (stderr is
shown to the model). Applies also inside driver sessions. Env POWERPUFF_ALLOW_PROTECTED=1 (set by the human) = allow.
See docs/attended-mode.md.
"""

import json
import os
import sys
from pathlib import Path

PROFILE = "powerpuff.yaml"
PROFILE_FILES = (PROFILE, "powerpuff.gates.json")
CASE_FOLD = os.name == "nt"


def find_root(cwd: Path) -> Path | None:
    """Nearest ancestor of cwd (inclusive) holding powerpuff.yaml; the search stops at the git root."""
    for d in (cwd, *cwd.parents):
        if (d / PROFILE).is_file():
            return d
        if (d / ".git").exists():
            return None
    return None


def hook_switches(root: Path) -> dict:
    """The `hooks:` section of the profile ({} when absent); cheap text pre-check before the YAML is parsed."""
    texts = [(root / n).read_text(encoding="utf-8", errors="replace") for n in PROFILE_FILES if (root / n).is_file()]
    if not any("hooks" in t for t in texts):
        return {}
    import gate_guard
    section = gate_guard.load_config(root, None).get("hooks")
    return section if isinstance(section, dict) else {}


def switch_on(section: dict, key: str) -> bool:
    value = section.get(key)
    return value is True or str(value).strip().lower() in ("true", "yes", "1")


def relative_target(root: Path, cwd: Path, raw: str) -> str | None:
    """Target path relative to the project root with forward slashes; None when it lies outside the root."""
    if CASE_FOLD:
        raw = raw.replace("\\", "/")
    p = Path(raw)
    p = (p if p.is_absolute() else cwd / p).resolve()
    try:
        rel = p.relative_to(root.resolve()).as_posix()
    except ValueError:
        return None
    return rel.lower() if CASE_FOLD else rel


def matching_glob(rel: str, globs: list[str]) -> str | None:
    import gate_guard
    for g in globs:
        if gate_guard.is_protected(rel, [g.lower() if CASE_FOLD else g]):
            return g
    return None


def frozen_paths(root: Path) -> list[str]:
    """Acceptance tests frozen by the current forge_run (`.forge/frozen.json`: {"files": {path: sha256}}), else []."""
    try:
        files = json.loads((root / ".forge" / "frozen.json").read_text(encoding="utf-8")).get("files")
    except (OSError, ValueError, AttributeError):
        return []
    return [f.lower() if CASE_FOLD else f for f in files] if isinstance(files, dict) else []


def run() -> int:
    if os.environ.get("POWERPUFF_ALLOW_PROTECTED") == "1":
        return 0
    event = json.loads(sys.stdin.read() or "{}")
    if not isinstance(event, dict):
        return 0
    cwd = Path(event.get("cwd") or ".").resolve()
    root = find_root(cwd)
    if root is None:
        return 0
    protect, frozen = switch_on(hook_switches(root), "protect"), frozen_paths(root)
    if not protect and not frozen:
        return 0
    tool_input = event.get("tool_input") or {}
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not raw:
        return 0
    rel = relative_target(root, cwd, str(raw))
    if rel is None:
        return 0
    if rel in frozen:
        print(f"powerpuff: edit of `{rel}` blocked - it is an acceptance test frozen for this run (`.forge/frozen.json`). "
              "Do not edit it; if it contradicts the spec, note it in `.forge/disputes.md` and implement the spec.",
              file=sys.stderr)
        return 2
    if not protect:
        return 0
    import gate_guard
    config = gate_guard.load_config(root, None)
    if rel == PROFILE:
        glob = PROFILE
    else:
        glob = matching_glob(rel, gate_guard.protected_globs(config))
    if glob is None:
        return 0
    print(f"powerpuff: edit of `{rel}` blocked - it is a protected file (matches `{glob}` in the gate profile). "
          "Ask the human to change it; do not work around it (no rename, no shell write, no other file).",
          file=sys.stderr)
    return 2


def main() -> int:
    try:
        return run()
    except Exception as e:  # noqa: BLE001 - a hook must never crash the session
        print(f"powerpuff hook error: {e}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
