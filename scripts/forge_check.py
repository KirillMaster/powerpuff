"""Deterministic done-check for /forge: the model does not decide `done`, this script does.

Checks (all must hold):
  coverage - every `R<n>` in .forge/requirements.md is referenced in at least one frozen acceptance test file
  frozen   - every file in .forge/frozen.sha256 is unchanged
  gates    - every command in .forge/state.json "gates" exits 0 (run from the repo root)
Prints JSON {"done": bool, "coverage": {...}, "frozen": {...}, "gates": {...}}; exit 0 when done, 1 otherwise.

Usage: python forge_check.py [--root .] [--skip-gates] [--timeout 1800]
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
R_LINE =re.compile(r"^\s*[-*]?\s*\**(R\d+)\**\s*[:.]", re.MULTILINE)


def frozen_files(root: Path) -> dict[str, str]:
    path = root / ".forge" / "frozen.sha256"
    out = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) == 2:
                out[parts[1].lstrip("*")] = parts[0]
    return out


def check_frozen(root: Path, files: dict[str, str]) -> dict:
    bad = []
    for rel, digest in files.items():
        p = root / rel
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            bad.append(rel)
    return {"ok": bool(files) and not bad, "files": len(files), "changed_or_missing": bad}


def check_coverage(root: Path, files: dict[str, str]) -> dict:
    req = root / ".forge" / "requirements.md"
    ids = sorted(set(R_LINE.findall(req.read_text(encoding="utf-8"))), key=lambda s: int(s[1:])) if req.is_file() else []
    text = "\n".join((root / f).read_text(encoding="utf-8", errors="replace") for f in files if (root / f).is_file())
    missing = [r for r in ids if not re.search(rf"\b{r}\b", text)]
    return {"ok": bool(ids) and not missing, "requirements": len(ids), "uncovered": missing}


def tool_missing(code: int, out: str) -> bool:
    low = out.lower()
    return code in (127, 9009) or "is not recognized as an internal or external command" in low \
        or "не является внутренней" in low


def gate_context(root: Path, base: str | None) -> dict[str, str]:
    """Values of the command placeholders: {base} (sha the unit started from), {root} (repo root) and {plugin}
    (plugin root, the directory with scripts/); paths with forward slashes."""
    return {"base": base or "", "root": str(root).replace(chr(92), "/"),
            "plugin": str(PLUGIN_ROOT).replace(chr(92), "/")}


def expand(cmd: str, context: dict[str, str] | None) -> str:
    """Substitutes {base} / {root} / {plugin} in a gate command; any other {...} text is left untouched."""
    ctx = {k: v for k, v in (context or {}).items() if v}
    return re.sub(r"\{(base|root|plugin)\}", lambda m: ctx.get(m.group(1), m.group(0)), cmd)


def context_env(context: dict[str, str] | None) -> dict[str, str] | None:
    """Gate environment: the POWERPUFF_* context plus UTF-8 stdio for Python tools (lizard, diff_gates), which
    otherwise crash with UnicodeEncodeError on a cp1252 Windows console when a path or message is non-ASCII."""
    utf8 = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    if not context:
        return {**os.environ, **utf8}
    return {**os.environ, **utf8, "POWERPUFF_BASE": context.get("base", ""), "POWERPUFF_ROOT": context.get("root", ""),
            "POWERPUFF_PLUGIN": context.get("plugin", "")}


def check_gates(root: Path, timeout: int, gates: dict | None = None, context: dict[str, str] | None = None,
                tail_chars: int = 600) -> dict:
    """Runs the gate commands; `gates` (id -> command) wins over .forge/state.json. A missing tool is a failure.
    `context` (see gate_context) fills {base}/{root}/{plugin} in the commands and is exported as POWERPUFF_BASE/_ROOT/
    _PLUGIN. Every result also has `exit_code` (null: tool missing or timeout) and `seconds` (wall time, 0.1)."""
    state = root / ".forge" / "state.json"
    if gates is None:
        gates = (json.loads(state.read_text(encoding="utf-8")).get("gates") or {}) if state.is_file() else {}
    results = {}
    env = context_env(context)
    for name, cmd in gates.items():
        t0 = time.monotonic()
        try:
            p = subprocess.run(expand(cmd, context), shell=True, cwd=root, capture_output=True, text=True,
                               timeout=timeout, encoding="utf-8", errors="replace", env=env)
            out = p.stdout + p.stderr
            results[name] = {"ok": p.returncode == 0, "exit_code": p.returncode,
                             "tail": out[-tail_chars:] if p.returncode else ""}
            if p.returncode and tool_missing(p.returncode, out):
                results[name]["missing_tool"] = True
        except subprocess.TimeoutExpired:
            results[name] = {"ok": False, "exit_code": None, "tail": f"timeout {timeout}s"}
        except FileNotFoundError as e:
            results[name] = {"ok": False, "exit_code": None, "missing_tool": True, "tail": str(e)[-tail_chars:]}
        results[name]["seconds"] = round(time.monotonic() - t0, 1)
    return {"ok": bool(gates) and all(r["ok"] for r in results.values()), "results": results}


def gate_row(name: str, tier: str, r: dict) -> dict:
    """One `gates` entry of quality-report.json from a check_gates result."""
    return {"name": name, "tier": tier, "ok": bool(r["ok"]), "exit_code": r.get("exit_code"),
            "seconds": r.get("seconds", 0.0), "missing_tool": bool(r.get("missing_tool")),
            "output_tail": r.get("tail", "")}


def quality_report(tool: str, head: str | None, base: str | None, approved: bool, tier: str, rows: list[dict],
                   **extra: object) -> dict:
    """quality-report.json, schema 1; the one builder for quality_report.py and forge_run.py."""
    return {"schema": 1, "tool": tool, "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "head": head or None, "base": base or None, "profile_approved": approved, "tier": tier, "gates": rows,
            "ok": all(r["ok"] for r in rows), **extra}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--skip-gates", action="store_true")
    ap.add_argument("--timeout", type=int, default=1800)
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    root = Path(a.root).resolve()
    files = frozen_files(root)
    report: dict = {"coverage": check_coverage(root, files), "frozen": check_frozen(root, files)}
    state = root / ".forge" / "state.json"
    start = (json.loads(state.read_text(encoding="utf-8")).get("start_sha") if state.is_file() else None) or "HEAD"
    report["gates"] = {"ok": True, "skipped": True} if a.skip_gates \
        else check_gates(root, a.timeout, context=gate_context(root, start))
    report["done"] = all(report[k]["ok"] for k in ("coverage", "frozen", "gates"))
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["done"] else 1


if __name__ == "__main__":
    sys.exit(main())
