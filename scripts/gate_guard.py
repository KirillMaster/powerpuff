"""Deterministic anti-tampering check over the git diff: the model proposes, this script decides.

Compares the working tree (committed + uncommitted + untracked changes) with a base commit. Only ADDED lines
and file-level changes are inspected. Rules:
  skip-added           - an added line introduces a test skip (test files; every file for inline-test languages)
  suppression-added    - an added line introduces a warning/coverage suppression (any file of a known language)
  protected-changed    - a protected config file was modified, deleted, renamed or newly added
  test-deleted         - a test file was deleted or renamed to a non-test path
  test-count-decreased - statically counted test declarations dropped versus the base
All language knowledge is data: the LANGUAGES table (id -> ext, test_file, skip, suppression, test_decl, optional
inline_tests) plus the stack-independent PROTECTED list. Pattern fields are regexes; `test_file` is matched
case-insensitively against the repo-relative path with forward slashes. `inline_tests: true` marks languages whose
tests live in production files (Rust): skip and counting then cover every file of that language.
Files of languages absent from the table get no skip/suppression checks; `protected-changed` still works and their
changed source-like extensions are listed under "unknown_extensions" (the guard is blind there).

Config (`powerpuff.yaml`, top-level keys): `protected` (list, adds globs), `unprotected` (list, removes default
globs), `allow_suppressions` (list of {rule, file (glob), pattern (substring, optional)}; matches are moved to
"allowed"), `languages` ({id: {ext, test_file, skip, suppression, test_decl, inline_tests}}; a new id adds a
language, an existing id replaces only the given fields). Run from the repository root.
Without PyYAML a minimal parser is used. It supports exactly: top-level `key:` with block lists of scalars, and
lists of flat mappings written as `- {rule: x, file: y}` or as `- rule: x` followed by indented `key: value`
lines; top-level scalars and flow lists `[a, b]`; one-level mappings (`gates:` -> `tier: [a, b]` or `tier:` plus
block list, `commands:` -> `id: command`). It CANNOT parse the nested `languages` shape: put it (or any other key) in the optional JSON file
`powerpuff.gates.json` next to the config; its top-level keys override the YAML ones.

Prints JSON {"ok": bool, "violations": [...], "allowed": [...], "counts": {rule: n}, "tests": {"before": n,
"after": n}, "unknown_extensions": [...]}. Exit 0 when ok, 1 on violations, 3 on input error (not a git repo,
unknown base, invalid language pattern).

Usage: python gate_guard.py --base <sha> [--root .] [--config powerpuff.yaml]
"""

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

PROTECTED = [
    "powerpuff.yaml", "powerpuff.gates.json", ".editorconfig", "Directory.Build.props", "Directory.Build.targets",
    "Directory.Packages.props", "quality/**", ".eslintrc*", "eslint.config.*", "tsconfig*.json", ".prettierrc*",
    "pyproject.toml", "ruff.toml", ".ruff.toml", "mypy.ini", "pyrightconfig.json", "setup.cfg", "pytest.ini",
    "tox.ini", "vitest.config.*", "jest.config.*", ".github/workflows/**", "stryker-config.json", ".globalconfig",
    "*.ruleset", "clippy.toml", ".clippy.toml", "rustfmt.toml", ".rustfmt.toml", "deny.toml", "rust-toolchain*",
    ".golangci.yml", ".golangci.yaml", ".golangci.toml", "checkstyle*.xml", "detekt*.yml", "spotbugs*.xml",
    "sonar-project.properties",
]

_JS_TEST_FILE = r"test|(^|[/._-])specs?([/._-]|$)"

LANGUAGES = {
    "csharp": {
        "ext": [".cs"],
        "test_file": [r"test"],
        "skip": [r"\[[^\]]*\bSkip\s*=", r"\[\s*(?:Ignore|Explicit)\b"],
        "suppression": [r"#\s*pragma\s+warning\s+disable", r"\[\s*(?:assembly:\s*|module:\s*)?SuppressMessage",
                        r"#\s*nullable\s+disable"],
        "test_decl": [r"\[\s*(?:Fact|Theory|Test|TestMethod|TestCase)\b"],
    },
    "msbuild": {
        "ext": [".csproj", ".props", ".targets"],
        "suppression": [r"<NoWarn"],
    },
    "javascript": {
        "ext": [".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"],
        "test_file": [_JS_TEST_FILE],
        "skip": [r"\.(?:skip|only)\(", r"\b(?:xit|xdescribe|xtest)\(", r"\b(?:it|test)\.todo\("],
        "suppression": [r"eslint-disable", r"@ts-(?:ignore|nocheck|expect-error)", r"istanbul\s+ignore",
                        r"c8\s+ignore"],
        "test_decl": [r"(?<![\w.])(?:it|test)(?:\.each\b|\()"],
    },
    "python": {
        "ext": [".py"],
        "test_file": [_JS_TEST_FILE],
        "skip": [r"@pytest\.mark\.(?:skip|skipif|xfail)\b", r"\bpytest\.skip\(", r"@unittest\.skip"],
        "suppression": [r"#\s*noqa\b", r"#\s*type:\s*ignore", r"#\s*pylint:\s*disable", r"#\s*pragma:\s*no\s*cover"],
        "test_decl": [r"(?m)^\s*(?:async\s+)?def test_"],
    },
    "rust": {
        "ext": [".rs"],
        "test_file": [r"(^|/)tests/.*\.rs$"],
        "inline_tests": True,
        "skip": [r"#\[ignore\b"],
        "suppression": [r"#!?\[allow\(", r"#\[cfg_attr\([^\]]*\ballow\b", r"//\s*nolint"],
        "test_decl": [r"#\[(?:test|tokio::test|rstest)\b"],
    },
    "go": {
        "ext": [".go"],
        "test_file": [r"_test\.go$"],
        "skip": [r"\bt\.(?:Skip|Skipf|SkipNow)\("],
        "suppression": [r"//\s*nolint"],
        "test_decl": [r"(?m)^func Test"],
    },
    "jvm": {
        "ext": [".java", ".kt", ".kts"],
        "test_file": [r"(^|/)src/test/", r"Tests?\.(?:java|kt)$"],
        "skip": [r"@(?:Disabled|Ignore)\b"],
        "suppression": [r"@SuppressWarnings", r"@Suppress\(", r"//\s*NOSONAR", r"//\s*noinspection"],
        "test_decl": [r"@(?:Test|ParameterizedTest)\b"],
    },
}

SOURCE_LIKE_EXT = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".m", ".mm", ".rb", ".php", ".swift", ".scala", ".dart", ".lua",
    ".ex", ".exs", ".erl", ".hs", ".clj", ".sh", ".ps1", ".pl", ".r", ".jl", ".vb", ".fs", ".zig", ".nim",
    ".groovy", ".go", ".rs", ".java", ".kt", ".cs", ".py", ".js", ".ts", ".tsx", ".jsx",
}
LANG_FIELDS = ("ext", "test_file", "skip", "suppression", "test_decl", "inline_tests")
EXCLUDED_DIRS = {"node_modules", "bin", "obj", ".git", "dist", "build", ".venv"}
SELF_FILES = {"gate_guard.py", "test_gate_guard.py"}
HUNK = re.compile(r"@@ -\S+ \+(\d+)(?:,\d+)? @@")


class GuardError(Exception):
    pass


def git(root: Path, *args: str) -> str:
    try:
        p = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=root, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except OSError as e:
        raise GuardError(f"cannot run git: {e}")
    if p.returncode:
        raise GuardError(p.stderr.strip() or f"git {args[0]} failed")
    return p.stdout


def ext(path: str) -> str:
    return Path(path).suffix.lower()


def patterns(values, flags: int = 0) -> list[re.Pattern]:
    try:
        return [re.compile(v, flags) for v in ([values] if isinstance(values, str) else values or [])]
    except (re.error, TypeError) as e:
        raise GuardError(f"invalid language pattern: {e}")


def build_languages(config: dict) -> dict[str, dict]:
    table = {lid: dict(fields) for lid, fields in LANGUAGES.items()}
    for lid, fields in (config.get("languages") or {}).items():
        if isinstance(fields, dict):
            table.setdefault(lid, {}).update({k: v for k, v in fields.items() if k in LANG_FIELDS})
    by_ext = {}
    for fields in table.values():
        lang = {"test_file": patterns(fields.get("test_file"), re.I), "inline_tests": bool(fields.get("inline_tests")),
                **{k: patterns(fields.get(k)) for k in ("skip", "suppression", "test_decl")}}
        exts = fields.get("ext") or []
        for e in [exts] if isinstance(exts, str) else exts:
            by_ext[str(e).lower()] = lang
    return by_ext


def is_test_file(path: str, lang: dict | None) -> bool:
    return bool(lang) and any(p.search(path) for p in lang["test_file"])


def holds_tests(path: str, lang: dict | None) -> bool:
    return bool(lang) and (lang["inline_tests"] or is_test_file(path, lang))


def matches(pats: list[re.Pattern], text: str) -> bool:
    return any(p.search(text) for p in pats)


def is_self(path: str) -> bool:
    p = Path(path)
    return p.name in SELF_FILES and (p.parent.name in ("scripts", "tests") or path in SELF_FILES)


def excluded(path: str) -> bool:
    return any(part in EXCLUDED_DIRS for part in path.split("/"))


def parse_added(diff: str):
    file, line, header = None, 0, False
    for raw in diff.splitlines():
        if raw.startswith("diff --git"):
            file, header = None, True
        elif header and raw.startswith("+++ "):
            p = raw[4:].split("\t")[0]
            file = p[2:] if p.startswith("b/") else None
        elif raw.startswith("@@"):
            header = False
            m = HUNK.match(raw)
            line = int(m.group(1)) if m else 0
        elif not header and file and raw.startswith("+"):
            yield file, line, raw[1:]
            line += 1


def untracked(root: Path) -> list[str]:
    out = git(root, "ls-files", "--others", "--exclude-standard")
    return [p for p in out.splitlines() if p and not excluded(p)]


def read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return None if b"\0" in data else data.decode("utf-8", errors="replace")


def added_lines(root: Path, base: str):
    yield from parse_added(git(root, "diff", "--unified=0", "-M", "--no-color", base))
    for rel in untracked(root):
        text = read_text(root / rel)
        for n, line in enumerate((text or "").splitlines(), 1):
            yield rel, n, line


def violation(rule: str, file: str, line: int, text: str) -> dict:
    return {"rule": rule, "file": file, "line": line, "text": text.strip()[:200]}


def check_lines(lines, by_ext: dict) -> list[dict]:
    out = []
    for file, n, text in lines:
        lang = by_ext.get(ext(file))
        if not lang or is_self(file):
            continue
        if holds_tests(file, lang) and matches(lang["skip"], text):
            out.append(violation("skip-added", file, n, text))
        if matches(lang["suppression"], text):
            out.append(violation("suppression-added", file, n, text))
    return out


def file_changes(root: Path, base: str) -> list[tuple[str, str, str | None]]:
    out = []
    for row in git(root, "diff", "--name-status", "-M", "--no-color", base).splitlines():
        parts = row.split("\t")
        if len(parts) >= 2:
            out.append((parts[0][0], parts[1], parts[2] if len(parts) > 2 else None))
    out += [("A", rel, None) for rel in untracked(root)]
    return out


def protected_globs(config: dict) -> list[str]:
    removed = set(config.get("unprotected") or [])
    return [g for g in PROTECTED if g not in removed] + list(config.get("protected") or [])


def is_protected(path: str, globs: list[str]) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(path if "/" in g else name, g) for g in globs)


def check_files(changes, globs: list[str], by_ext: dict) -> list[dict]:
    out = []
    labels = {"M": "modified", "D": "deleted", "R": "renamed", "A": "added", "C": "added", "T": "modified"}
    for status, path, new in changes:
        paths = [path, new] if status == "R" and new else [new if status == "C" and new else path]
        for p in paths:
            if is_protected(p, globs):
                out.append(violation("protected-changed", p, 0, labels.get(status, "modified")))
        if status == "D" and is_test_file(path, by_ext.get(ext(path))):
            out.append(violation("test-deleted", path, 0, "deleted"))
        elif status == "R" and new and is_test_file(path, by_ext.get(ext(path))) \
                and not is_test_file(new, by_ext.get(ext(new))):
            out.append(violation("test-deleted", path, 0, f"renamed to {new}"))
    return out


def unknown_extensions(changes, by_ext: dict) -> list[str]:
    paths = {p for _, old, new in changes for p in (old, new) if p}
    return sorted({ext(p) for p in paths if ext(p) in SOURCE_LIKE_EXT and ext(p) not in by_ext})


def count_tests(text: str, lang: dict) -> int:
    return sum(len(p.findall(text)) for p in lang["test_decl"])


def tests_before(root: Path, base: str, by_ext: dict) -> int:
    total = 0
    for rel in git(root, "ls-tree", "-r", "--name-only", base).splitlines():
        lang = by_ext.get(ext(rel))
        if holds_tests(rel, lang) and not excluded(rel):
            total += count_tests(git(root, "show", f"{base}:{rel}"), lang)
    return total


def tests_after(root: Path, by_ext: dict) -> int:
    total = 0
    for rel in sorted(set(git(root, "ls-files", "--cached", "--others", "--exclude-standard").splitlines())):
        lang = by_ext.get(ext(rel))
        if rel and holds_tests(rel, lang) and not excluded(rel) and (root / rel).is_file():
            total += count_tests(read_text(root / rel) or "", lang)
    return total


def allowed(v: dict, rules: list[dict]) -> bool:
    for r in rules:
        if not isinstance(r, dict) or r.get("rule") != v["rule"]:
            continue
        if not fnmatch.fnmatchcase(v["file"], str(r.get("file") or "*")):
            continue
        if str(r.get("pattern") or "") in v["text"]:
            return True
    return False


def unquote(s: str) -> str:
    s = s.strip()
    return s[1:-1] if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'" else s


def split_flow(body: str) -> list[str]:
    parts, cur, quote = [], "", ""
    for ch in body:
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == ",":
            parts, cur = parts + [cur], ""
            continue
        cur += ch
    return parts + [cur]


def strip_comment(s: str) -> str:
    s = s.strip()
    return s if s[:1] in "\"'" else re.sub(r"\s+#.*$", "", s)


def is_flow_list(s: str) -> bool:
    return s.startswith("[") and s.endswith("]")


def flow_list(s: str) -> list[str]:
    return [unquote(p) for p in split_flow(s[1:-1]) if p.strip()]


def parse_pair(s: str) -> tuple[str, str] | None:
    m = re.match(r"^([\w-]+):\s*(.*)$", s.strip())
    return (m.group(1), unquote(strip_comment(m.group(2)))) if m else None


def parse_simple_yaml(text: str) -> dict:
    result: dict = {}
    key, sub, item, dash_indent = None, None, None, 0
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        body = raw.strip()
        top = indent == 0 and not body.startswith("-")
        if top:
            m = re.match(r"^([\w-]+):\s*(.*)$", body)
            key, sub, item = (m.group(1), None, None) if m else (None, None, None)
            if m:
                val = strip_comment(m.group(2))
                result[key] = flow_list(val) if is_flow_list(val) else unquote(val) if val else []
        elif key and isinstance(result.get(key), list) and body.startswith("-"):
            val = body[1:].strip()
            dash_indent = indent
            if val.startswith("{") and val.endswith("}"):
                pairs = [parse_pair(p) for p in split_flow(val[1:-1])]
                result[key].append({k: v for k, v in filter(None, pairs)})
                item = None
            elif parse_pair(val) and not re.match(r"^[\"'].*[\"']$", val):
                k, v = parse_pair(val)
                item = {k: v}
                result[key].append(item)
            else:
                result[key].append(unquote(val))
                item = None
        elif key and item is None and not body.startswith("-") and parse_pair(body) \
                and (isinstance(result.get(key), dict) or result.get(key) == []):
            k, v = parse_pair(body)
            result[key] = result[key] if isinstance(result[key], dict) else {}
            sub = k
            result[key][k] = flow_list(v) if is_flow_list(v) else v if v else []
        elif key and sub and isinstance(result.get(key), dict) and isinstance(result[key].get(sub), list) \
                and body.startswith("-"):
            result[key][sub].append(unquote(body[1:].strip()))
        elif item is not None and indent > dash_indent and parse_pair(body):
            k, v = parse_pair(body)
            item[k] = v
    return result


def load_config(root: Path, path: str | None) -> dict:
    p = Path(path or "powerpuff.yaml")
    p = p if p.is_absolute() else root / p
    data: dict = {}
    if p.is_file():
        text = p.read_text(encoding="utf-8", errors="replace")
        try:
            import yaml
            loaded = yaml.safe_load(text)
        except ImportError:
            loaded = parse_simple_yaml(text)
        data = loaded if isinstance(loaded, dict) else {}
    extra = p.with_name("powerpuff.gates.json")
    if extra.is_file():
        try:
            loaded = json.loads(extra.read_text(encoding="utf-8", errors="replace"))
        except ValueError as e:
            raise GuardError(f"invalid {extra.name}: {e}")
        data.update(loaded if isinstance(loaded, dict) else {})
    return data


def check(root: Path, base: str, config: dict | None = None) -> dict:
    root = Path(root)
    config = config or {}
    if base.startswith("-"):
        raise GuardError(f"invalid base: {base}")
    if git(root, "rev-parse", "--is-inside-work-tree").strip() != "true":
        raise GuardError("not a git work tree")
    git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    by_ext = build_languages(config)
    changes = file_changes(root, base)
    found = check_lines(added_lines(root, base), by_ext) + check_files(changes, protected_globs(config), by_ext)
    before, after = tests_before(root, base, by_ext), tests_after(root, by_ext)
    if after < before:
        found.append(violation("test-count-decreased", "", 0, f"{before} -> {after}"))
    rules = config.get("allow_suppressions") or []
    violations = [v for v in found if not allowed(v, rules)]
    counts: dict[str, int] = {}
    for v in violations:
        counts[v["rule"]] = counts.get(v["rule"], 0) + 1
    return {"ok": not violations, "violations": violations, "allowed": [v for v in found if v not in violations],
            "counts": counts, "tests": {"before": before, "after": after},
            "unknown_extensions": unknown_extensions(changes, by_ext)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--root", default=".")
    ap.add_argument("--config")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    root = Path(a.root).resolve()
    try:
        report = check(root, a.base, load_config(root, a.config))
    except GuardError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 3
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
