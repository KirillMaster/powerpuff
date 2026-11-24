"""Draft `powerpuff.yaml` from the stack presets (presets/*.yaml): deterministic, no model.

Detection: a preset's `detect` markers (file globs, or `glob~text` = file whose content has the text) are looked for
in the repo root and its subdirectories up to depth 2. Several stacks / several project dirs = a monorepo: gate ids
get a prefix (`dotnet-build`, `web-lint`) and commands of subprojects are `cd <dir> && ...`. Repo-wide gates
(`global` in the preset: duplication, size) are emitted once.
Filtering: a preset gate is kept only when its `needs` condition holds in the repo (tool on PATH, config file,
dependency, package.json script, tests present, other gate kept); dropped gates are listed as recommendations.
`needs` value = terms joined by `+` (all must hold); a term = `kind:alt|alt|kind2:alt` (any alt); kinds: tool, file,
has (`glob~text`), dep (package.json / node_modules/.bin), script (package.json), tests (glob over the project's
files), gate / no-gate (gate ids already kept in this project). A bare alt takes the kind of the term.
Placeholders resolved here: {pm} / {pmx} (npm | pnpm | yarn by lock file); {plugin} only with --plugin DIR.
{plugin}, {base} and {root} stay for the driver, so the profile is portable. The draft is `approved: false`: a human reviews it and sets `approved: true`.

Exit codes: 0 draft produced (written with --write), 1 --write refused (a profile already exists, never overwritten),
2 no preset for this repo (a skeleton with `commands: {}` is produced).

Usage: python profile_init.py [--repo .] [--write] [--stack name,...] [--plugin DIR]
"""

import argparse
import fnmatch
import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_guard  # noqa: E402

PLUGIN = Path(__file__).resolve().parent.parent
PRESETS_DIR = PLUGIN / "presets"
PROFILE_FILES = ("powerpuff.yaml", "powerpuff.gates.json")
TIERS = ("iteration", "unit", "final")
SKIP_DIRS = {"node_modules", "bin", "obj", ".git", "dist", "build", "target", "vendor", ".venv", "venv", "env",
             "__pycache__", ".forge", ".tox", ".mypy_cache", ".pytest_cache", "site-packages", "TestResults"}
DETECT_DEPTH = 2
FILE_DEPTH = 6
MAX_FILES = 20000
MAX_READ = 400_000
KINDS = ("tool", "file", "has", "dep", "script", "tests", "gate", "no-gate")
KIND_RE = re.compile(r"^(" + "|".join(KINDS) + r"):")
NO_TESTS_SCRIPT = "no test specified"
DOCS = "docs/profile.md"


def load_presets(directory: Path = PRESETS_DIR) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in sorted(directory.glob("*.yaml")):
        cfg = gate_guard.load_config(directory, p.name)
        if isinstance(cfg.get("gates"), dict) and isinstance(cfg.get("commands"), dict):
            out[str(cfg.get("stack") or p.stem)] = cfg
    return out


def listing(value: object) -> list[str]:
    return [str(x) for x in value] if isinstance(value, list) else []


def matches(rel: str, pattern: str) -> bool:
    rel, pattern = rel.lower(), pattern.lower()
    if "/" in pattern:
        return fnmatch.fnmatchcase(rel, pattern) or fnmatch.fnmatchcase(rel, "*/" + pattern)
    return fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], pattern)


def walk(root: Path, depth: int) -> list[str]:
    """Repo-relative posix paths of files up to `depth` directory levels, junk directories pruned."""
    out: list[str] = []
    base = len(root.parts)
    for cur, dirs, names in os.walk(root):
        level = len(Path(cur).parts) - base
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (d.startswith(".") and d != ".github")) \
            if level < depth else []
        rel = Path(cur).relative_to(root).as_posix()
        out += [n if rel == "." else f"{rel}/{n}" for n in sorted(names)]
        if len(out) > MAX_FILES:
            break
    return out


def read_text(path: Path) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(MAX_READ)
    except OSError:
        return ""


class Project:
    """One stack in one directory (`rel` = '' for the repo root)."""

    def __init__(self, root: Path, stack: str, rel: str, preset: dict):
        self.root, self.stack, self.rel, self.preset = root, stack, rel, preset
        self.dir = root / rel if rel else root
        self.files = walk(self.dir, FILE_DEPTH)
        self.kept: list[str] = []
        self._pkg: dict | None = None

    def pkg(self) -> dict:
        if self._pkg is None:
            try:
                data = json.loads(read_text(self.dir / "package.json"))
            except ValueError:
                data = {}
            self._pkg = data if isinstance(data, dict) else {}
        return self._pkg

    def pm(self) -> str:
        for d in (self.dir, self.root):
            if (d / "pnpm-lock.yaml").is_file():
                return "pnpm"
            if (d / "yarn.lock").is_file():
                return "yarn"
        return "npm"

    def has_alt(self, kind: str, alt: str) -> bool:
        if kind == "tool":
            return bool(shutil.which(alt) or shutil.which(alt, path=str(self.dir / "node_modules" / ".bin")))
        if kind in ("file", "tests"):
            return any(matches(f, alt) for f in self.files)
        if kind == "has":
            glob, _, text = alt.partition("~")
            hits = [f for f in self.files if matches(f, glob)][:300]
            return any(text.lower() in read_text(self.dir / f).lower() for f in hits)
        if kind == "dep":
            pkg = self.pkg()
            declared = {*(pkg.get("dependencies") or {}), *(pkg.get("devDependencies") or {})}
            return alt in declared or any((self.dir / "node_modules" / ".bin" / (alt.rsplit("/", 1)[-1] + ext)).is_file()
                                          for ext in ("", ".cmd"))
        if kind == "script":
            val = (self.pkg().get("scripts") or {}).get(alt)
            return isinstance(val, str) and bool(val.strip()) and NO_TESTS_SCRIPT not in val
        if kind == "gate":
            return alt in self.kept
        return alt not in self.kept  # no-gate

    def missing(self, need: str) -> str | None:
        """None when every term of `need` holds, else the first failing term."""
        for term in (t.strip() for t in need.split("+") if t.strip()):
            kind = ""
            ok = False
            for alt in term.split("|"):
                m = KIND_RE.match(alt)
                if m:
                    kind, alt = m.group(1), alt[m.end():]
                elif not kind:
                    kind = "tool"
                if self.has_alt(kind, alt):
                    ok = True
                    break
            if not ok:
                return term
        return None


def find_projects(root: Path, presets: dict[str, dict], forced: list[str] | None = None) -> list[Project]:
    if forced:
        return [Project(root, s, "", presets[s]) for s in forced if s in presets]
    names = walk(root, DETECT_DEPTH)
    found: list[Project] = []
    for stack, preset in presets.items():
        dirs: set[str] = set()
        for marker in listing(preset.get("detect")):
            glob, _, text = marker.partition("~")
            for f in names:
                if matches(f, glob) and (not text or text.lower() in read_text(root / f).lower()):
                    dirs.add(f.rsplit("/", 1)[0] if "/" in f else "")
        for d in sorted(dirs, key=lambda x: (x.count("/") if x else -1, x)):
            if not any(o == "" or d == o or d.startswith(o + "/") for o in (p.rel for p in found if p.stack == stack)):
                found.append(Project(root, stack, d, preset))
    return sorted(found, key=lambda p: (p.rel != "", p.rel, p.stack))


def slug(text: str) -> str:
    return re.sub(r"[^\w-]+", "-", text).strip("-") or "root"


def prefixes(projects: list[Project]) -> dict[int, str]:
    """Gate-id prefix per project: none for a single root project, else the stack (root) or the directory name."""
    if len(projects) == 1 and not projects[0].rel:
        return {0: ""}
    out: dict[int, str] = {}
    taken: set[str] = set()
    for i, p in enumerate(projects):
        name = p.stack if not p.rel else slug(p.rel)
        if name in taken:
            name = f"{name}-{p.stack}"
        taken.add(name)
        out[i] = name + "-"
    return out


def render_command(cmd: str, p: Project, plugin: str | None, scoped: bool = True) -> str:
    if plugin:
        cmd = cmd.replace("{plugin}", plugin)
    cmd = cmd.replace("{pmi}", {"npm": "npm install --no-audit --no-fund", "pnpm": "pnpm install",
                                "yarn": "yarn install"}[p.pm()])
    cmd = cmd.replace("{pmx}", {"npm": "npx --no-install", "pnpm": "pnpm exec", "yarn": "yarn"}[p.pm()]) \
        .replace("{pm}", p.pm())
    if not p.rel or not scoped:
        return cmd
    cmd = re.sub(r"(diff_gates\.py\" \w+)", lambda m: m.group(1) + " --root {root}", cmd)
    if "diff_gates.py\" coverage" in cmd:
        cmd += " --missing-file ignore"
    return f"cd {p.rel} && {cmd}"


def rooted(cmd: str) -> str:
    """`cd <rel> && x` -> `cd "{root}/<rel>" && x`: joined setup/format_fix steps must not inherit the previous step's
    directory, and cmd.exe (shell=True on Windows) does not isolate `cd` inside parentheses."""
    return re.sub(r"^cd (\S+) && ", lambda m: f'cd "{{root}}/{m.group(1)}" && ', cmd)


def build(root: Path, projects: list[Project], plugin: str | None = None) -> dict:
    pre = prefixes(projects)
    gates: dict[str, list[str]] = {t: [] for t in TIERS}
    commands: dict[str, str] = {}
    fixes: list[str] = []
    setups: list[str] = []
    protected: list[str] = []
    test_gates: list[str] = []
    dropped: list[tuple[str, str, str]] = []
    emitted_global: set[str] = set()
    for i, p in enumerate(projects):
        needs = p.preset.get("needs") if isinstance(p.preset.get("needs"), dict) else {}
        globals_ = listing(p.preset.get("global"))
        for tier in TIERS:
            for gid in listing((p.preset.get("gates") or {}).get(tier)):
                cmd = (p.preset.get("commands") or {}).get(gid)
                if not isinstance(cmd, str) or (gid in globals_ and gid in emitted_global):
                    continue
                miss = p.missing(str(needs[gid])) if gid in needs else None
                name = gid if gid in globals_ else pre[i] + gid
                if miss:
                    if "no-gate:" not in str(needs.get(gid)):
                        dropped.append((name, render_command(cmd, p, plugin), miss))
                    continue
                p.kept.append(gid)
                gates[tier].append(name)
                commands[name] = render_command(cmd, p, plugin, scoped=gid not in globals_)
                if gid in globals_:
                    emitted_global.add(gid)
                if gid in listing(p.preset.get("test_gates")):
                    test_gates.append(name)
        fix = p.preset.get("format_fix")
        if isinstance(fix, str) and not (needs.get("format_fix") and p.missing(str(needs["format_fix"]))):
            fixes.append(rooted(render_command(fix, p, plugin)))
        setup = p.preset.get("setup")
        if isinstance(setup, str) and not (needs.get("setup") and p.missing(str(needs["setup"]))):
            setups.append(rooted(render_command(setup, p, plugin)))
        for g in listing(p.preset.get("protected")):
            for item in ([g] if not p.rel else [f"{p.rel}/{g}", g]):
                if item not in protected:
                    protected.append(item)
    return {"gates": gates, "commands": commands, "format_fix": " && ".join(fixes) or None,
            "setup": " && ".join(setups) or None, "protected": protected,
            "test_gates": test_gates, "dropped": dropped}


def yq(s: str) -> str:
    return f"'{s}'" if "'" not in s else json.dumps(s, ensure_ascii=False)


def key(k: str) -> str:
    return k if re.fullmatch(r"[\w-]+", k) else yq(k)


def render(projects: list[Project], draft: dict) -> str:
    where = ", ".join(f"{p.stack} ({p.rel or '.'})" for p in projects)
    stacks = sorted({p.stack for p in projects})
    lines = ["# powerpuff.yaml - DRAFT: generated by scripts/profile_init.py from the repo manifests, no model involved.",
             "# A human must review the gates, commands and thresholds and set approved: true;",
             "# while it is false, run reports are marked 'gates not approved'. See: " + DOCS,
             f"# Detected: {where}", "approved: false", f"stack: {stacks[0] if len(stacks) == 1 else 'mixed'}", "gates:"]
    for tier in TIERS:
        if draft["gates"][tier]:
            names = ", ".join(g if re.fullmatch(r"[\w-]+", g) else yq(g) for g in draft["gates"][tier])
            lines.append(f"  {tier}: [{names}]")
    lines.append("commands:")
    lines += [f"  {key(g)}: {yq(c)}" for g, c in draft["commands"].items()]
    if draft["format_fix"]:
        lines.append(f"format_fix: {yq(draft['format_fix'])}")
    if draft.get("setup"):
        lines.append(f"setup: {yq(draft['setup'])}")
    if draft["test_gates"]:
        lines.append(f"test_gates: [{', '.join(draft['test_gates'])}]")
    lines.append(f"protected: [{', '.join(yq(g) for g in draft['protected'])}]")
    if draft["dropped"]:
        lines += ["", "# recommended to add (the repo has no tool or config for these):"]
        lines += [f"#   {n}: {c}   [needs: {m}]" for n, c, m in draft["dropped"]]
    return "\n".join(lines) + "\n"


SKELETON = """# powerpuff.yaml - DRAFT SKELETON: no preset for this repository, the profile is written by hand.
# How: """ + DOCS + """ (section "Stack without a preset"). A human sets approved: true after review.
approved: false
stack: unknown
gates:
  iteration: []
  unit: []
commands: {}
protected: []
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--write", action="store_true", help="write powerpuff.yaml when no profile exists")
    ap.add_argument("--stack", help="comma-separated stacks instead of detection (project = repo root)")
    ap.add_argument("--plugin", help="plugin root substituted for {plugin} (default: stays {plugin}, the driver expands it)")
    a = ap.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    root = Path(a.repo).resolve()
    presets = load_presets()
    forced = [s.strip() for s in a.stack.split(",") if s.strip()] if a.stack else None
    projects = find_projects(root, presets, forced)
    draft = build(root, projects, a.plugin.replace(chr(92), "/") if a.plugin else None) if projects else None
    text = render(projects, draft) if draft and draft["commands"] else SKELETON
    print(text, end="")
    rc = 0 if draft and draft["commands"] else 2
    if rc:
        print(f"no preset - the profile is written by hand: {DOCS}", file=sys.stderr)
    if a.write:
        existing = [n for n in PROFILE_FILES if (root / n).is_file()]
        if existing:
            print(f"profile already exists ({', '.join(existing)}), not overwritten", file=sys.stderr)
            return 1
        (root / "powerpuff.yaml").write_text(text, encoding="utf-8")
        print(f"written: {root / 'powerpuff.yaml'} (approved: false)", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
