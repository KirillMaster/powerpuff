"""Run the gates of a profile tier and write `quality-report.json` (schema 1): the script decides, not a model.

The profile (`powerpuff.yaml`) is read by forge_run's loader; every gate of the tier is run on its own through
forge_check.check_gates (so {base} / {root} work as in the driver) and timed. A missing tool is a failure.
quality-report.json: {"schema": 1, "tool": "powerpuff.quality_report", "generated_at", "head", "base",
"profile_approved", "tier", "gates": [{"name", "tier", "ok", "exit_code", "seconds", "missing_tool",
"output_tail"}], "ok"}. `exit_code` is the real one (null: tool missing or timeout), `seconds` is measured by check_gates.
Prints a table gate / result / seconds. Exit codes: 0 all green, 1 a gate is red, 3 no profile, bad profile or a
missing tool.

Usage: python quality_report.py --repo . [--tier iteration|unit|final|all] [--base <sha>] [--out quality-report.json]
       [--timeout 1800]
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_check  # noqa: E402
import forge_run  # noqa: E402

TIERS = ("iteration", "unit", "final")


def git(root: Path, *args: str) -> str:
    try:
        p = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def tier_gates(prof: dict, tiers: list[str]) -> list[tuple[str, str, str]]:
    """(tier, gate id, command) in profile order."""
    lists = prof["config"].get("gates") or {}
    cmds = prof["config"].get("commands") or {}
    return [(t, g, cmds[g]) for t in tiers for g in lists.get(t, [])]


def run_gates(root: Path, items: list[tuple[str, str, str]], ctx: dict[str, str], timeout: int) -> list[dict]:
    return [forge_check.gate_row(name, tier, forge_check.check_gates(root, timeout, {name: cmd}, ctx,
                                                                      tail_chars=2000)["results"][name])
            for tier, name, cmd in items]


def table(rows: list[dict]) -> str:
    width = max([len(r["name"]) for r in rows] + [4])
    lines = [f"{'gate'.ljust(width)}  {'result':<12} seconds"]
    for r in rows:
        res = "pass" if r["ok"] else "MISSING TOOL" if r["missing_tool"] else "FAIL"
        lines.append(f"{r['name'].ljust(width)}  {res:<12} {r['seconds']:.1f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--tier", choices=(*TIERS, "all"), default="all")
    ap.add_argument("--base", help="commit the unit started from ({base}); default HEAD (empty diff)")
    ap.add_argument("--out", default="quality-report.json")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds per gate")
    a = ap.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    root = Path(a.repo).resolve()
    try:
        prof = forge_run.load_profile(root)
    except ValueError as e:
        print(f"quality_report: {e}", file=sys.stderr)
        return 3
    if not prof:
        print("quality_report: no profile (powerpuff.yaml); run scripts/profile_init.py", file=sys.stderr)
        return 3
    head = git(root, "rev-parse", "HEAD")
    base = (git(root, "rev-parse", "--verify", "-q", a.base + "^{commit}") or a.base) if a.base else head
    tiers = list(TIERS) if a.tier == "all" else [a.tier]
    items = tier_gates(prof, tiers)
    if not items:
        print(f"quality_report: the profile lists no gates in tier {a.tier}", file=sys.stderr)
        return 3
    if not prof["approved"]:
        print("quality_report: profile is NOT approved (approved: false)", file=sys.stderr)
    rows = run_gates(root, items, forge_check.gate_context(root, base), a.timeout)
    report = forge_check.quality_report("powerpuff.quality_report", head, base, prof["approved"], a.tier, rows)
    out = Path(a.out) if Path(a.out).is_absolute() else root / a.out
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(table(rows))
    print(f"quality-report: {out} ok={report['ok']}")
    if any(r["missing_tool"] for r in rows):
        return 3
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
