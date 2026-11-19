"""Stack-agnostic gates on the DIFF of one unit of work: the core knows report formats, not stacks.

Each gate is an ordinary profile command: the exit code decides. 0 pass, 1 fail, 3 cannot evaluate (missing or
unparseable report, not a git repo, unknown format, ambiguous path match) - a missing report is a failure, never a pass.
  coverage    - coverage of ADDED/CHANGED executable lines (git diff vs base) from a cobertura / lcov / go coverprofile /
                coverage.py json / jacoco report; test files are ignored; a changed code file absent from every report
                counts as fully uncovered (--missing-file fail, default)
  duplication - new copy-paste: a window of --min-lines normalised lines made only of added lines that also occurs
                elsewhere in the tracked code files (another file, or elsewhere in the same file)
  complexity  - touched functions whose cyclomatic complexity exceeds --max (lizard csv/xml, eslint json, radon json,
                gocyclo, sarif); with --base-report a function that is not worse than on base passes
  size        - budget of changed lines / files of the unit, tests and lock/generated files excluded
  all         - runs the gates configured in the `diff_gates` section of the profile, writes a quality-report.json
                fragment (--out)
Language knowledge (test files, code extensions) is reused from gate_guard; the config (`--config`, default
powerpuff.yaml) is loaded the same way. Without PyYAML the `diff_gates` section is read by a small built-in parser
(two-level mapping, `gate:` plus indented `key: value` lines or `gate: {key: value}`; lists only as `[a, b]`).

Usage: python diff_gates.py <gate> --base <sha> [--root .] [options] [--json]
"""

import argparse
import csv
import fnmatch
import glob
import io
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote as url_unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_guard as gg  # noqa: E402

MSBUILD_EXT = {".csproj", ".props", ".targets"}
DUP_EXCLUDED_DIRS = {"node_modules", "bin", "obj", "dist", "build", "target", "vendor", ".git"}
GENERATED = re.compile(r"(\.min\.(js|css)|\.(g|generated|designer)\.cs|\.generated\.\w+|_pb2(_grpc)?\.py|\.pb\.go|\.d\.ts)$",
                       re.I)
LOCK = re.compile(r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|pnpm-lock\.yaml|go\.sum|[^/]+\.lock)$", re.I)
MAX_BYTES = 1_000_000
MAX_DETAILS = 50
HASH, DASH, SLASH = ("#",), ("--",), ("//", "/*", "*/")
COMMENTS = {**{e: HASH for e in (".py", ".rb", ".sh", ".ps1", ".pl", ".r", ".jl", ".ex", ".exs", ".nim")},
            **{e: DASH for e in (".lua", ".hs")}, ".erl": ("%",), ".clj": (";",), ".vb": ("'",),
            **{e: SLASH for e in (".cs", ".java", ".kt", ".kts", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go",
                                  ".rs", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".m", ".mm", ".swift", ".scala",
                                  ".dart", ".zig", ".groovy", ".php", ".fs")}}
FALLBACK_COMMENT = ("#", "//")
PUNCT = re.compile(r"^[{}()\[\];,]*$")
TRIVIAL = re.compile(r"^(?:[{}()\[\];,]*|end;?|else:?|\} ?else \{?|begin|fi|done|esac|"
                     r"(?:import|using|package|namespace|use|require|include|from)\b.*|#include\b.*)$")
COV_FORMATS = ("auto", "cobertura", "lcov", "go", "coveragepy-json", "jacoco")
CX_FORMATS = ("auto", "lizard-csv", "lizard-xml", "eslint-json", "radon-json", "gocyclo", "sarif")
GATE_IDS = {"coverage": "diff-coverage", "duplication": "duplication", "complexity": "complexity", "size": "size"}
GOCYCLO = re.compile(r"^(\d+)\s+(\S+)\s+(\S+)\s+(.+):(\d+):(\d+)\s*$")
GO_BLOCK = re.compile(r"^(.+):(\d+)\.\d+,(\d+)\.\d+ \d+ (\d+)$")


class GateError(Exception):
    pass


def norm_path(p: str) -> str:
    p = p.strip().replace("\\", "/")
    if p.lower().startswith("file://"):
        p = url_unquote(p[7:])
        if re.match(r"^/[A-Za-z]:/", p):
            p = p[1:]
    while p.startswith("./"):
        p = p[2:]
    return re.sub(r"/{2,}", "/", p)


def skipped(path: str, patterns) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(path, g) or fnmatch.fnmatchcase(name, g) for g in patterns)


def ranges(nums) -> str:
    out: list[list[int]] = []
    for n in sorted(nums):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in out)


def significant(text: str, ext: str) -> str | None:
    s = re.sub(r"\s+", " ", text.strip())
    return None if not s or s.startswith(COMMENTS.get(ext, FALLBACK_COMMENT)) else s


class Diff:
    """The unit of work: added lines (new-side numbers) per file versus the base, plus repo/language knowledge."""

    def __init__(self, root: Path, base: str, config: dict):
        self.root, self.base = root, base
        try:
            if base.startswith("-"):
                raise GateError(f"invalid base: {base}")
            if gg.git(root, "rev-parse", "--is-inside-work-tree").strip() != "true":
                raise GateError("not a git work tree")
            gg.git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
            self.by_ext = gg.build_languages(config)
            self.added: dict[str, dict[int, str]] = {}
            for f, n, t in gg.added_lines(root, base):
                self.added.setdefault(f, {})[n] = t
        except gg.GuardError as e:
            raise GateError(str(e))
        self.code_exts = {e for e in self.by_ext if e not in MSBUILD_EXT}
        self.dup_exts = self.code_exts | (gg.SOURCE_LIKE_EXT - MSBUILD_EXT)
        self._files: list[str] | None = None
        self.frozen = frozen_files(root)

    def files(self) -> list[str]:
        if self._files is None:
            try:
                out = gg.git(self.root, "ls-files", "--cached", "--others", "--exclude-standard")
            except gg.GuardError as e:
                raise GateError(str(e))
            self._files = sorted({f for f in out.splitlines() if f and (self.root / f).is_file()})
        return self._files

    def is_test(self, path: str) -> bool:
        return gg.is_test_file(path, self.by_ext.get(gg.ext(path)))

    def is_prod(self, path: str, exclude=()) -> bool:
        return gg.ext(path) in self.code_exts and not self.is_test(path) and not gg.excluded(path) \
            and not skipped(path, exclude)


class Resolver:
    """Maps report paths to repo files: absolute/backslash/`file://`, go.mod module prefix, subproject-relative."""

    def __init__(self, d: Diff):
        self.files = set(d.files())
        self.changed = set(d.added)
        self.by_base: dict[str, list[str]] = {}
        self.mods: list[tuple[str, str]] = []
        for f in self.files:
            self.by_base.setdefault(f.rsplit("/", 1)[-1], []).append(f)
            if f.rsplit("/", 1)[-1] == "go.mod":
                m = re.search(r"^module\s+(\S+)", gg.read_text(d.root / f) or "", re.M)
                if m:
                    self.mods.append((f.rsplit("/", 1)[0] if "/" in f else "", m.group(1).strip("\"")))
        self.ambiguous: dict[str, list[str]] = {}

    def resolve(self, paths) -> str | None:
        for p in paths:
            r = self._one(norm_path(p))
            if r:
                return r
        return None

    def _one(self, e: str) -> str | None:
        cands = self.by_base.get(e.rsplit("/", 1)[-1], [])
        if e.endswith(".go"):
            for dir_, mod in self.mods:
                if e.startswith(mod + "/"):
                    c = (dir_ + "/" if dir_ else "") + e[len(mod) + 1:]
                    if c in self.files:
                        return c
        longer = [r for r in cands if e == r or e.endswith("/" + r)]
        if longer:
            return max(longer, key=len)
        if re.match(r"^(/|[A-Za-z]:/)", e):
            return None
        rel = [r for r in cands if r.endswith("/" + e)]
        if len(rel) > 1:
            if any(r in self.changed for r in rel):
                self.ambiguous[e] = sorted(rel)
            return None
        return rel[0] if rel else None

    def check(self) -> None:
        if self.ambiguous:
            raise GateError("ambiguous report paths: " + "; ".join(f"{e} -> {', '.join(v)}"
                                                                   for e, v in sorted(self.ambiguous.items())))


def read_reports(root: Path, spec: str) -> list[tuple[Path, bytes]]:
    pattern = spec if Path(spec).is_absolute() else str(root / spec)
    paths = [pattern] if Path(pattern).is_file() else sorted(glob.glob(pattern, recursive=True))
    paths = [Path(p) for p in paths if Path(p).is_file()]
    if not paths:
        raise GateError(f"report not found: {spec}")
    try:
        return [(p, p.read_bytes()) for p in paths]
    except OSError as e:
        raise GateError(f"cannot read report: {e}")


def text_of(data: bytes) -> str:
    return data.decode("utf-8-sig", errors="replace")


def parse_report(fn, data: bytes, path):
    try:
        return fn(data)
    except GateError:
        raise
    except (ValueError, KeyError, IndexError, TypeError, AttributeError, ET.ParseError) as e:
        raise GateError(f"unparseable report {path}: {e}")


# ---- coverage report parsers: [(candidate paths, {line: hits})]

def cov_cobertura(data: bytes):
    root = ET.fromstring(data)
    sources = [(s.text or "").strip().replace("\\", "/").rstrip("/") for s in root.iter("source")]
    out = []
    for c in root.iter("class"):
        fn = c.get("filename")
        if not fn:
            continue
        lines: dict[int, int] = {}
        for ln in c.iter("line"):
            n = int(ln.get("number"))
            lines[n] = max(lines.get(n, 0), int(float(ln.get("hits", 0))))
        out.append(([f"{s}/{fn}" for s in sources if s] + [fn], lines))
    return out


def cov_lcov(data: bytes):
    out, cur, lines = [], None, {}
    for raw in text_of(data).splitlines():
        raw = raw.strip()
        if raw.startswith("SF:"):
            cur, lines = raw[3:], {}
        elif raw.startswith("DA:") and cur is not None:
            n, hits = raw[3:].split(",")[:2]
            lines[int(n)] = max(lines.get(int(n), 0), int(float(hits)))
        elif raw == "end_of_record" and cur is not None:
            out.append(([cur], lines))
            cur = None
    return out


def cov_go(data: bytes):
    files: dict[str, dict[int, int]] = {}
    for raw in text_of(data).splitlines():
        m = GO_BLOCK.match(raw.strip())
        if m:
            lines = files.setdefault(m.group(1), {})
            for n in range(int(m.group(2)), int(m.group(3)) + 1):
                lines[n] = max(lines.get(n, 0), int(m.group(4)))
    return [([p], lines) for p, lines in files.items()]


def cov_coveragepy(data: bytes):
    out = []
    for path, info in json.loads(text_of(data))["files"].items():
        lines = {int(n): 1 for n in info.get("executed_lines", [])}
        lines.update({int(n): 0 for n in info.get("missing_lines", [])})
        out.append(([path], lines))
    return out


def cov_jacoco(data: bytes):
    out = []
    for pkg in ET.fromstring(data).iter("package"):
        for sf in pkg.iter("sourcefile"):
            path = f"{pkg.get('name', '')}/{sf.get('name')}".lstrip("/")
            out.append(([path], {int(ln.get("nr")): int(ln.get("ci", 0)) for ln in sf.iter("line")}))
    return out


COV_PARSERS = {"cobertura": cov_cobertura, "lcov": cov_lcov, "go": cov_go, "coveragepy-json": cov_coveragepy,
               "jacoco": cov_jacoco}


def detect_coverage(data: bytes) -> str:
    text = text_of(data).lstrip()
    if text.startswith("{"):
        return "coveragepy-json"
    if text.startswith("<"):
        m = re.search(r"<(report|coverage)[\s>]", text)
        if m:
            return "jacoco" if m.group(1) == "report" else "cobertura"
    if text.startswith("mode:"):
        return "go"
    if text.startswith("TN:") or re.search(r"^SF:", text, re.M):
        return "lcov"
    raise GateError("unknown coverage report format")


def gate_coverage(d: Diff, report: str, fmt: str = "auto", min_pct: float = 80.0, min_lines: int = 1, exclude=(),
                  missing_file: str = "fail") -> dict:
    if fmt not in COV_FORMATS or missing_file not in ("ignore", "fail"):
        raise GateError(f"invalid coverage option: format={fmt} missing-file={missing_file}")
    entries = []
    for path, data in read_reports(d.root, report):
        entries += parse_report(COV_PARSERS[fmt if fmt != "auto" else detect_coverage(data)], data, path)
    if not entries:
        raise GateError("coverage report has no data")
    res = Resolver(d)
    cov: dict[str, dict[int, int]] = {}
    for paths, lines in entries:
        rel = res.resolve(paths)
        if rel:
            cur = cov.setdefault(rel, {})
            for n, h in lines.items():
                cur[n] = max(cur.get(n, 0), h)
    res.check()
    details, covered, total = [], 0, 0
    for rel in sorted(d.added):
        if not d.is_prod(rel, exclude):
            continue
        amap = d.added[rel]
        if rel in cov:
            exe = {n for n in amap if n in cov[rel]}
            unc = {n for n in exe if cov[rel][n] <= 0}
            note = ""
        elif missing_file == "ignore":
            details.append({"file": rel, "note": "absent from report, ignored"})
            continue
        else:
            ext = gg.ext(rel)
            exe = {n for n, t in amap.items() if (s := significant(t, ext)) and not PUNCT.match(s)}
            unc, note = set(exe), "absent from report: counted as uncovered"
        total += len(exe)
        covered += len(exe) - len(unc)
        if unc:
            details.append({"file": rel, "uncovered": ranges(unc), "executable": len(exe), **({"note": note} if note else {})})
    pct = round(100.0 * covered / total, 1) if total else None
    ok = total == 0 or total < min_lines or pct >= min_pct
    if total == 0:
        details.append({"note": "no changed executable lines"})
    elif total < min_lines:
        details.append({"note": f"only {total} changed executable lines (< min-lines {min_lines})"})
    return {"gate": "coverage", "ok": ok, "value": pct, "threshold": min_pct, "details": details}


# ---- duplication

def frozen_files(root: Path) -> set[str]:
    """Acceptance tests frozen by forge_run (`.forge/frozen.json`): the writer may not edit them, so clones inside them are not its debt."""
    try:
        return set((json.loads((root / ".forge" / "frozen.json").read_text(encoding="utf-8")).get("files") or {}))
    except (OSError, ValueError, AttributeError):
        return set()


def dup_ok(d: Diff, rel: str, exclude) -> bool:
    return gg.ext(rel) in d.dup_exts and not any(p in DUP_EXCLUDED_DIRS for p in rel.split("/")) \
        and not GENERATED.search(rel) and not skipped(rel, exclude)


def prepare(d: Diff, rel: str):
    p = d.root / rel
    try:
        if p.stat().st_size > MAX_BYTES:
            return None
    except OSError:
        return None
    text = gg.read_text(p)
    if text is None:
        return None
    ext = gg.ext(rel)
    nums, norms = [], []
    for n, line in enumerate(text.split("\n"), 1):
        s = significant(line, ext)
        if s:
            nums.append(n)
            norms.append(s)
    return nums, norms


def windows(norms: list[str], k: int) -> list[int]:
    hs = [hash(s) for s in norms]
    return [hash(tuple(hs[i:i + k])) for i in range(len(hs) - k + 1)]


def gate_duplication(d: Diff, min_lines: int = 6, max_new: int = 0, exclude=()) -> dict:
    k = int(min_lines)
    if k < 1:
        raise GateError("min-lines must be >= 1")
    cand: dict[int, list[tuple[str, int]]] = {}
    nums_of: dict[str, list[int]] = {}
    prepared: dict[str, tuple] = {}
    for rel in sorted(d.added):
        p = prepare(d, rel) if dup_ok(d, rel, exclude) and rel not in d.frozen else None
        if not p:
            continue
        nums, norms = p
        prepared[rel], nums_of[rel] = p, nums
        amap = d.added[rel]
        is_added = [n in amap for n in nums]
        triv = [bool(TRIVIAL.match(s)) for s in norms]
        for i, h in enumerate(windows(norms, k)):
            if all(is_added[i:i + k]) and not all(triv[i:i + k]):
                cand.setdefault(h, []).append((rel, i))
    occ: dict[int, list[tuple[str, int]]] = {}
    for rel in d.files() if cand else []:
        if not dup_ok(d, rel, exclude):
            continue
        p = prepared.get(rel) or prepare(d, rel)
        if not p:
            continue
        for i, h in enumerate(windows(p[1], k)):
            if h in cand:
                occ.setdefault(h, []).append((rel, i))
                nums_of[rel] = p[0]
    groups: dict[tuple, list[int]] = {}
    for h, mine in cand.items():
        for fa, ia in mine:
            for fb, ib in occ.get(h, []):
                if not (fa == fb and abs(ia - ib) < k):
                    groups.setdefault((fa, fb, ib - ia), []).append(ia)
    clones: dict[tuple, int] = {}
    for (fa, fb, delta), starts in groups.items():
        starts.sort()
        run = [starts[0]]
        for s in starts[1:] + [None]:
            if s is not None and s == run[-1] + 1:
                run.append(s)
                continue
            i0, i1 = run[0], run[-1]
            a = (fa, nums_of[fa][i0], nums_of[fa][i1 + k - 1])
            b = (fb, nums_of[fb][i0 + delta], nums_of[fb][i1 + delta + k - 1])
            clones[tuple(sorted([a, b]))] = i1 - i0 + k
            run = [s]
    found = sorted(clones.items())
    details = [{"clone": f"{a[0]}:{a[1]}-{a[2]} ↔ {b[0]}:{b[1]}-{b[2]} ({n} lines)", "lines": n}
               for (a, b), n in found[:MAX_DETAILS]]
    if len(found) > MAX_DETAILS:
        details.append({"note": f"{len(found) - MAX_DETAILS} more clone pairs not listed"})
    return {"gate": "duplication", "ok": len(found) <= max_new, "value": len(found), "threshold": max_new,
            "details": details}


# ---- complexity report parsers: [{file, name, start, end|None, ccn}]

def cx_lizard_csv(data: bytes):
    out = []
    for row in csv.reader(io.StringIO(text_of(data))):
        if len(row) >= 11 and row[1].strip().isdigit():
            out.append({"file": row[6], "name": row[7], "start": int(row[9]), "end": int(row[10]), "ccn": int(row[1])})
    if not out:
        raise GateError("no functions in lizard csv")
    return out


def cx_lizard_xml(data: bytes):
    out = []
    for meas in ET.fromstring(data).iter("measure"):
        if meas.get("type") != "Function":
            continue
        labels = [(lb.text or "").strip() for lb in meas.iter("label")]
        for item in meas.iter("item"):
            vals = [int(v.text) for v in item.findall("value")]
            off = len(vals) - len(labels)
            m = re.match(r"^(.*) at (.+):(\d+)$", item.get("name", ""))
            if not m:
                continue
            start = int(m.group(3))
            length = vals[labels.index("length") + off] if "length" in labels else 1
            out.append({"file": m.group(2), "name": m.group(1), "start": start, "end": start + max(length, 1) - 1,
                        "ccn": vals[labels.index("CCN") + off]})
    if not out:
        raise GateError("no functions in lizard xml")
    return out


def cx_eslint(data: bytes):
    out = []
    for entry in json.loads(text_of(data)):
        for m in entry.get("messages", []):
            c = re.search(r"complexity of (\d+)", m.get("message", ""))
            if m.get("ruleId") == "complexity" and c:
                nm = re.search(r"'([^']+)'", m["message"])
                out.append({"file": entry["filePath"], "name": nm.group(1) if nm else "<anonymous>", "start": m["line"],
                            "end": m.get("endLine") or m["line"], "ccn": int(c.group(1))})
    return out


def cx_radon(data: bytes):
    out = []

    def walk(path, items):
        for it in items:
            if it.get("type") in ("function", "method"):
                name = f"{it['classname']}.{it['name']}" if it.get("classname") else it["name"]
                out.append({"file": path, "name": name, "start": it["lineno"], "end": it.get("endline") or it["lineno"],
                            "ccn": it["complexity"]})
            walk(path, it.get("methods") or [])
            walk(path, it.get("closures") or [])

    for path, items in json.loads(text_of(data)).items():
        if isinstance(items, list):
            walk(path, items)
    return out


def cx_gocyclo(data: bytes):
    out = []
    for raw in text_of(data).splitlines():
        m = GOCYCLO.match(raw.strip())
        if m:
            out.append({"file": m.group(4), "name": f"{m.group(2)}.{m.group(3)}", "start": int(m.group(5)), "end": None,
                        "ccn": int(m.group(1))})
    return out


def cx_sarif(data: bytes):
    out = []
    for run in json.loads(text_of(data)).get("runs", []):
        rules = run.get("tool", {}).get("driver", {}).get("rules", [])
        for r in run.get("results", []):
            rid, idx = r.get("ruleId") or "", r.get("ruleIndex")
            rname = rules[idx].get("name", "") if isinstance(idx, int) and 0 <= idx < len(rules) else ""
            if "complex" not in f"{rid} {rname}".lower():
                continue
            msg = (r.get("message") or {}).get("text", "")
            m = re.search(r"(?:complexity|ccn)\D{0,30}?(\d+)", msg, re.I) or re.search(r"(\d+)", msg)
            nm = re.search(r"'([^']+)'", msg)
            for loc in (r.get("locations") or [])[:1]:
                pl = loc["physicalLocation"]
                reg = pl.get("region", {})
                if m:
                    out.append({"file": pl["artifactLocation"]["uri"], "name": nm.group(1) if nm else rid,
                                "start": reg["startLine"], "end": reg.get("endLine") or reg["startLine"],
                                "ccn": int(m.group(1))})
    return out


CX_PARSERS = {"lizard-csv": cx_lizard_csv, "lizard-xml": cx_lizard_xml, "eslint-json": cx_eslint,
              "radon-json": cx_radon, "gocyclo": cx_gocyclo, "sarif": cx_sarif}


def detect_complexity(data: bytes) -> str:
    text = text_of(data).lstrip()
    if text.startswith("<"):
        return "lizard-xml"
    if text[:1] in "[{":
        loaded = json.loads(text)
        return "eslint-json" if isinstance(loaded, list) else "sarif" if "runs" in loaded else "radon-json"
    first = text.splitlines()[0] if text else ""
    if first.count(",") >= 10:
        return "lizard-csv"
    if GOCYCLO.match(first.strip()):
        return "gocyclo"
    raise GateError("unknown complexity report format")


def load_functions(d: Diff, spec: str, fmt: str) -> list[dict]:
    if fmt not in CX_FORMATS:
        raise GateError(f"invalid complexity format: {fmt}")
    funcs: list[dict] = []
    for path, data in read_reports(d.root, spec):
        try:
            f = fmt if fmt != "auto" else detect_complexity(data)
        except ValueError as e:
            raise GateError(f"unparseable report {path}: {e}")
        funcs += parse_report(CX_PARSERS[f], data, path)
    res = Resolver(d)
    out = []
    for fn in funcs:
        rel = res.resolve([fn["file"]])
        if rel:
            out.append({**fn, "file": rel})
    res.check()
    return out


def gate_complexity(d: Diff, report: str, fmt: str = "auto", max_ccn: int = 15, base_report: str | None = None,
                    exclude=()) -> dict:
    funcs = load_functions(d, report, fmt)
    base: dict[tuple, int] = {}
    for fn in load_functions(d, base_report, fmt) if base_report else []:
        key = (fn["file"], fn["name"])
        base[key] = max(base.get(key, 0), fn["ccn"])
    details, worst, bad = [], 0, 0
    for fn in sorted(funcs, key=lambda f: (f["file"], f["start"])):
        rel = fn["file"]
        if rel not in d.added or not d.is_prod(rel, exclude):
            continue
        end = fn["end"] if fn["end"] is not None else fn["start"]
        if not any(fn["start"] <= n <= end for n in d.added[rel]):
            continue
        worst = max(worst, fn["ccn"])
        if fn["ccn"] <= max_ccn:
            continue
        prev = base.get((rel, fn["name"]))
        row = {"file": rel, "function": fn["name"], "line": fn["start"], "ccn": fn["ccn"]}
        if fn["end"] is None:
            row["note"] = "report has only the start line: range = start line"
        if prev is not None and fn["ccn"] <= prev:
            details.append({**row, "kind": "pre-existing", "base_ccn": prev})
        else:
            bad += 1
            details.append({**row, "kind": "violation", **({"base_ccn": prev} if prev is not None else {})})
    return {"gate": "complexity", "ok": bad == 0, "value": worst, "threshold": max_ccn, "details": details}


# ---- size

def numstat(d: Diff) -> dict[str, int]:
    try:
        tokens = gg.git(d.root, "diff", "--numstat", "-z", "-M", "--no-color", d.base).split("\0")
        untracked = gg.untracked(d.root)
    except gg.GuardError as e:
        raise GateError(str(e))
    out: dict[str, int] = {}
    i = 0
    while i < len(tokens):
        if not tokens[i]:
            i += 1
            continue
        a, dl, path = tokens[i].split("\t", 2)
        i += 1
        if not path:
            path, i = tokens[i + 1], i + 2
        if a != "-" and dl != "-":
            out[path] = out.get(path, 0) + int(a) + int(dl)
    for rel in untracked:
        out[rel] = len((gg.read_text(d.root / rel) or "").splitlines())
    return out


def gate_size(d: Diff, max_lines: int = 400, max_files: int = 25, exclude=()) -> dict:
    counted = {p: n for p, n in numstat(d).items()
               if not d.is_test(p) and not LOCK.search(p) and not GENERATED.search(p) and not skipped(p, exclude)}
    lines, files = sum(counted.values()), len(counted)
    details = [{"changed_lines": lines, "max_lines": max_lines, "files": files, "max_files": max_files}]
    if lines > max_lines:
        details.append({"over": f"lines {lines} > {max_lines}"})
    if files > max_files:
        details.append({"over": f"files {files} > {max_files}"})
    return {"gate": "size", "ok": lines <= max_lines and files <= max_files, "value": lines, "threshold": max_lines,
            "details": details}


# ---- dispatch, profile, CLI

def num(o: dict, key: str, default):
    try:
        v = float(o.get(key, default))
    except (TypeError, ValueError):
        raise GateError(f"{key}: not a number: {o.get(key)}")
    return int(v) if v == int(v) else v


def as_list(v) -> list[str]:
    return [] if v is None else [v] if isinstance(v, str) else [str(x) for x in v]


def run_gate(d: Diff, name: str, o: dict) -> dict:
    ex = as_list(o.get("exclude"))
    if name in ("coverage", "complexity") and not o.get("report"):
        raise GateError(f"{name}: report is not set")
    if name == "coverage":
        return gate_coverage(d, str(o["report"]), o.get("format", "auto"), float(num(o, "min", 80)),
                             int(num(o, "min_lines", 1)), ex, o.get("missing_file", "fail"))
    if name == "duplication":
        return gate_duplication(d, int(num(o, "min_lines", 6)), int(num(o, "max_new", 0)), ex)
    if name == "complexity":
        br = o.get("base_report")
        return gate_complexity(d, str(o["report"]), o.get("format", "auto"), int(num(o, "max", 15)),
                               str(br) if br else None, ex)
    if name == "size":
        return gate_size(d, int(num(o, "max_lines", 400)), int(num(o, "max_files", 25)), ex)
    raise GateError(f"unknown gate: {name}")


def error_result(gate: str, msg: str) -> dict:
    return {"gate": gate, "ok": False, "value": None, "threshold": None, "details": [{"error": msg}], "error": msg}


def scalar(v: str):
    if gg.is_flow_list(v):
        return gg.flow_list(v)
    if re.fullmatch(r"-?\d+(\.\d+)?", v):
        return float(v) if "." in v else int(v)
    return {"true": True, "false": False}.get(v.lower(), v)


def parse_section(text: str) -> dict:
    out: dict = {}
    inside, level, gate = False, None, None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent, body = len(raw) - len(raw.lstrip()), raw.strip()
        if indent == 0:
            inside, level, gate = body.startswith("diff_gates:"), None, None
            continue
        pair = gg.parse_pair(body) if inside else None
        if not pair:
            continue
        level = indent if level is None else level
        k, v = pair
        if indent <= level:
            gate = k
            out[k] = {}
            if v.startswith("{") and v.endswith("}"):
                out[k] = {a: scalar(b) for a, b in filter(None, (gg.parse_pair(p) for p in gg.split_flow(v[1:-1])))}
        elif gate is not None:
            out[gate][k] = scalar(v)
    return out


def profile_section(root: Path, config_path: str | None, config: dict) -> dict:
    section = config.get("diff_gates")
    if isinstance(section, dict) and all(isinstance(v, dict) for v in section.values()):
        return section
    p = Path(config_path or "powerpuff.yaml")
    p = p if p.is_absolute() else root / p
    return parse_section(p.read_text(encoding="utf-8", errors="replace")) if p.is_file() else {}


def fragment(results: list[dict]) -> dict:
    gates = []
    for r in results:
        g = {"id": GATE_IDS.get(r["gate"], r["gate"]), "status": "passed" if r["ok"] else "failed", "blocking": True,
             "value": r["value"], "threshold": r["threshold"]}
        if r.get("error"):
            g["error"] = r["error"]
        gates.append(g)
    return {"schema": 1, "tool": "diff_gates", "gates": gates}


def render(r: dict) -> str:
    lines = [f"{r['gate']}: {'PASS' if r['ok'] else 'FAIL'}  value={r['value']}  threshold={r['threshold']}"]
    lines += ["  " + "  ".join(f"{k}={v}" for k, v in row.items()) for row in r["details"]]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base", required=True)
    common.add_argument("--root", default=".")
    common.add_argument("--config")
    common.add_argument("--json", action="store_true")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="gate", required=True)
    cov = sub.add_parser("coverage", parents=[common])
    cov.add_argument("--report", required=True)
    cov.add_argument("--format", choices=COV_FORMATS)
    cov.add_argument("--min", type=float)
    cov.add_argument("--min-lines", dest="min_lines", type=int)
    cov.add_argument("--missing-file", dest="missing_file", choices=("ignore", "fail"))
    dup = sub.add_parser("duplication", parents=[common])
    dup.add_argument("--min-lines", dest="min_lines", type=int)
    dup.add_argument("--max-new", dest="max_new", type=int)
    cx = sub.add_parser("complexity", parents=[common])
    cx.add_argument("--report", required=True)
    cx.add_argument("--format", choices=CX_FORMATS)
    cx.add_argument("--max", type=int)
    cx.add_argument("--base-report", dest="base_report")
    sz = sub.add_parser("size", parents=[common])
    sz.add_argument("--max-lines", dest="max_lines", type=int)
    sz.add_argument("--max-files", dest="max_files", type=int)
    al = sub.add_parser("all", parents=[common])
    al.add_argument("--out")
    for p in (cov, dup, sz):
        p.add_argument("--exclude", action="append")
    cx.add_argument("--exclude", action="append")
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    root = Path(a.root).resolve()
    opts = {k: v for k, v in vars(a).items() if v is not None}
    try:
        config = gg.load_config(root, a.config)
        d = Diff(root, a.base, config)
        if a.gate != "all":
            results = [run_gate(d, a.gate, opts)]
        else:
            section = profile_section(root, a.config, config)
            if not section:
                raise GateError("no diff_gates section in the profile")
            results = []
            for name, o in section.items():
                try:
                    results.append(run_gate(d, name, {k.replace("-", "_"): v for k, v in o.items()}))
                except GateError as e:
                    results.append(error_result(name, str(e)))
    except (GateError, gg.GuardError) as e:
        results = [error_result(a.gate, str(e))]
    errored = any(r.get("error") for r in results)
    ok = all(r["ok"] for r in results)
    if a.gate == "all":
        frag = fragment(results)
        if a.out:
            out = Path(a.out) if Path(a.out).is_absolute() else root / a.out
            out.write_text(json.dumps(frag, ensure_ascii=False, indent=1), encoding="utf-8")
        payload = {"ok": ok and not errored, "results": results, "fragment": frag}
    else:
        payload = results[0]
    if a.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        print("\n".join(render(r) for r in results))
    return 3 if errored else 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
