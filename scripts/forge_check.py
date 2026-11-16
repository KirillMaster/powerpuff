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
import re
import subprocess
import sys
from pathlib import Path

R_LINE = re.compile(r"^\s*[-*]?\s*\**(R\d+)\**\s*[:.]", re.MULTILINE)


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


def check_gates(root: Path, timeout: int) -> dict:
    state = root / ".forge" / "state.json"
    gates = (json.loads(state.read_text(encoding="utf-8")).get("gates") or {}) if state.is_file() else {}
    results = {}
    for name, cmd in gates.items():
        try:
            p = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True, timeout=timeout,
                               encoding="utf-8", errors="replace")
            results[name] = {"ok": p.returncode == 0, "tail": (p.stdout + p.stderr)[-600:] if p.returncode else ""}
        except subprocess.TimeoutExpired:
            results[name] = {"ok": False, "tail": f"timeout {timeout}s"}
    return {"ok": bool(gates) and all(r["ok"] for r in results.values()), "results": results}


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
    report["gates"] = {"ok": True, "skipped": True} if a.skip_gates else check_gates(root, a.timeout)
    report["done"] = all(report[k]["ok"] for k in ("coverage", "frozen", "gates"))
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["done"] else 1


if __name__ == "__main__":
    sys.exit(main())
