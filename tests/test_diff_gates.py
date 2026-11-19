import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "diff_gates"
sys.path.insert(0, str(SCRIPTS))

import diff_gates as dg  # noqa: E402

FORMATS = ["cobertura", "lcov", "go", "coveragepy-json", "jacoco"]
PY_BASE = "def add(a, b):\n    return a + b\n"
PY_ADDED = "def sub(a, b):\n    r = a - b\n    return r\n"


def git(root: Path, *args: str) -> str:
    p = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def append(root: Path, rel: str, text: str) -> None:
    write(root, rel, (root / rel).read_text(encoding="utf-8") + text)


def edit_line(root: Path, rel: str, n: int, new: str) -> None:
    lines = (root / rel).read_text(encoding="utf-8").split("\n")
    lines[n - 1] = new
    write(root, rel, "\n".join(lines))


def commit(root: Path, msg: str = "c") -> str:
    git(root, "add", "-A")
    git(root, "commit", "--no-gpg-sign", "-q", "-m", msg)
    return git(root, "rev-parse", "HEAD")


def init(tmp_path: Path, files: dict[str, str]):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "core.autocrlf", "false")
    for rel, text in files.items():
        write(root, rel, text)
    return root, commit(root, "base")


def run(capsys, root: Path, base: str, *args: str):
    capsys.readouterr()
    code = dg.main([*args[:1], "--base", base, "--root", str(root), "--json", *args[1:]])
    out = capsys.readouterr().out
    return code, json.loads(out)


def block(prefix: str, n: int = 8, indent: str = "") -> str:
    return "".join(f"{indent}{prefix}_{i} = compute({i})\n" for i in range(n))


def write_report(tmp_path: Path, fmt: str, path: str, hits: dict[int, int]) -> Path:
    name = {"cobertura": "c.xml", "lcov": "l.info", "go": "g.out", "coveragepy-json": "c.json", "jacoco": "j.xml"}[fmt]
    if fmt == "cobertura":
        lines = "".join(f'<line number="{n}" hits="{h}"/>' for n, h in hits.items())
        body = (f'<?xml version="1.0"?><coverage><packages><package name="p"><classes><class name="c" '
                f'filename="{path}"><lines>{lines}</lines></class></classes></package></packages></coverage>')
    elif fmt == "lcov":
        body = f"SF:{path}\n" + "".join(f"DA:{n},{h}\n" for n, h in hits.items()) + "end_of_record\n"
    elif fmt == "go":
        body = "mode: set\n" + "".join(f"{path}:{n}.1,{n}.20 1 {h}\n" for n, h in hits.items())
    elif fmt == "coveragepy-json":
        body = json.dumps({"files": {path: {"executed_lines": [n for n, h in hits.items() if h],
                                            "missing_lines": [n for n, h in hits.items() if not h]}}})
    else:
        pkg, _, fname = path.rpartition("/")
        lines = "".join(f'<line nr="{n}" mi="{0 if h else 1}" ci="{h}" mb="0" cb="0"/>' for n, h in hits.items())
        body = f'<?xml version="1.0"?><report name="r"><package name="{pkg}"><sourcefile name="{fname}">{lines}</sourcefile></package></report>'
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def cov_repo(tmp_path: Path, rel: str = "src/calc.py"):
    root, base = init(tmp_path, {rel: PY_BASE, "README.md": "x\n"})
    append(root, rel, PY_ADDED)
    return root, base


def prefixed(fmt: str, path: str) -> str:
    return ("example.com/m/" if fmt == "go" else "") + path


ALL_COVERED = {1: 1, 2: 1, 3: 1, 4: 1, 5: 1}


# ---- coverage

@pytest.mark.parametrize("fmt", FORMATS)
def test_coverage_uncovered_added_line_fails(tmp_path, capsys, fmt):
    root, base = cov_repo(tmp_path)
    rep = write_report(tmp_path, fmt, prefixed(fmt, "src/calc.py"), {1: 1, 2: 1, 3: 1, 4: 0, 5: 1})
    code, data = run(capsys, root, base, "coverage", "--report", str(rep), "--format", fmt)
    assert code == 1 and not data["ok"]
    assert data["value"] == 66.7 and data["threshold"] == 80
    assert data["details"][0]["file"] == "src/calc.py" and data["details"][0]["uncovered"] == "4"


@pytest.mark.parametrize("fmt", FORMATS)
def test_coverage_covered_added_lines_pass(tmp_path, capsys, fmt):
    root, base = cov_repo(tmp_path)
    rep = write_report(tmp_path, fmt, prefixed(fmt, "src/calc.py"), ALL_COVERED)
    code, data = run(capsys, root, base, "coverage", "--report", str(rep))
    assert code == 0 and data["ok"] and data["value"] == 100.0


def test_coverage_old_uncovered_untouched_lines_never_fail(tmp_path, capsys):
    root, base = cov_repo(tmp_path)
    rep = write_report(tmp_path, "lcov", "src/calc.py", {1: 0, 2: 0, 3: 1, 4: 1, 5: 1})
    code, data = run(capsys, root, base, "coverage", "--report", str(rep))
    assert code == 0 and data["value"] == 100.0


def test_coverage_lines_absent_from_report_are_not_executable(tmp_path, capsys):
    root, base = cov_repo(tmp_path)
    rep = write_report(tmp_path, "lcov", "src/calc.py", {1: 1, 2: 1, 3: 1, 5: 1})
    code, data = run(capsys, root, base, "coverage", "--report", str(rep))
    assert code == 0 and data["value"] == 100.0


def test_coverage_missing_report_is_3(tmp_path, capsys):
    root, base = cov_repo(tmp_path)
    code, data = run(capsys, root, base, "coverage", "--report", str(tmp_path / "nope.info"))
    assert code == 3 and not data["ok"] and "not found" in data["error"]


def test_coverage_unparseable_and_unknown_format_are_3(tmp_path, capsys):
    root, base = cov_repo(tmp_path)
    junk = tmp_path / "junk.txt"
    junk.write_text("hello world\n", encoding="utf-8")
    assert run(capsys, root, base, "coverage", "--report", str(junk))[0] == 3
    assert run(capsys, root, base, "coverage", "--report", str(junk), "--format", "lcov")[0] == 3
    bad = tmp_path / "bad.xml"
    bad.write_text("<coverage><oops>", encoding="utf-8")
    assert run(capsys, root, base, "coverage", "--report", str(bad))[0] == 3


def test_coverage_not_a_git_repo_is_3(tmp_path, capsys):
    rep = tmp_path / "l.info"
    rep.write_text("SF:a.py\nDA:1,1\nend_of_record\n", encoding="utf-8")
    plain = tmp_path / "plain"
    plain.mkdir()
    code, data = run(capsys, plain, "HEAD", "coverage", "--report", str(rep))
    assert code == 3 and not data["ok"]


def test_coverage_changed_file_absent_from_report(tmp_path, capsys):
    root, base = cov_repo(tmp_path)
    write(root, "src/new.py", "def f():\n    x = 1\n    return x\n\n# note\n")
    write(root, "docs/notes.md", "# a\nb\nc\n")
    rep = write_report(tmp_path, "lcov", "src/calc.py", ALL_COVERED)
    code, data = run(capsys, root, base, "coverage", "--report", str(rep))
    assert code == 1
    row = next(r for r in data["details"] if r.get("file") == "src/new.py")
    assert row["uncovered"] == "1-3" and "absent" in row["note"]
    assert all(r.get("file") != "docs/notes.md" for r in data["details"])
    code, data = run(capsys, root, base, "coverage", "--report", str(rep), "--missing-file", "ignore")
    assert code == 0 and any("ignored" in r.get("note", "") for r in data["details"])


def test_coverage_test_files_ignored(tmp_path, capsys):
    root, base = cov_repo(tmp_path)
    write(root, "tests/test_calc.py", "def test_a():\n    assert 1\n")
    rep = write_report(tmp_path, "lcov", "src/calc.py", ALL_COVERED)
    code, data = run(capsys, root, base, "coverage", "--report", str(rep))
    assert code == 0 and data["value"] == 100.0


def test_coverage_exclude_glob(tmp_path, capsys):
    root, base = cov_repo(tmp_path)
    rep = write_report(tmp_path, "lcov", "src/calc.py", {1: 1, 2: 1, 3: 1, 4: 0, 5: 1})
    code, _ = run(capsys, root, base, "coverage", "--report", str(rep), "--exclude", "src/*")
    assert code == 0


def test_coverage_no_changed_executable_lines_passes_with_note(tmp_path, capsys):
    root, base = init(tmp_path, {"src/calc.py": PY_BASE})
    write(root, "README.md", "hello\n")
    rep = write_report(tmp_path, "lcov", "src/calc.py", ALL_COVERED)
    code, data = run(capsys, root, base, "coverage", "--report", str(rep))
    assert code == 0 and any("no changed executable" in r.get("note", "") for r in data["details"])


def test_coverage_report_glob_merges_reports(tmp_path, capsys):
    root, base = cov_repo(tmp_path)
    (tmp_path / "reps").mkdir()
    (tmp_path / "reps" / "a.info").write_text("SF:src/calc.py\nDA:3,1\nDA:4,0\nend_of_record\n", encoding="utf-8")
    (tmp_path / "reps" / "b.info").write_text("SF:src/calc.py\nDA:4,2\nDA:5,1\nend_of_record\n", encoding="utf-8")
    code, data = run(capsys, root, base, "coverage", "--report", str(tmp_path / "reps" / "*.info"))
    assert code == 0 and data["value"] == 100.0


@pytest.mark.parametrize("report_path", ["ABS", "src\\calc.py", "C:\\x\\repo\\src\\calc.py", "file:///C:/x/repo/src/calc.py"])
def test_coverage_path_forms(tmp_path, capsys, report_path):
    root, base = cov_repo(tmp_path)
    path = str(root / "src" / "calc.py") if report_path == "ABS" else report_path
    rep = write_report(tmp_path, "lcov", path, {1: 1, 2: 1, 3: 1, 4: 0, 5: 1})
    code, data = run(capsys, root, base, "coverage", "--report", str(rep))
    assert code == 1 and data["details"][0]["file"] == "src/calc.py"


def test_coverage_cobertura_sources_root(tmp_path, capsys):
    root, base = cov_repo(tmp_path)
    rep = tmp_path / "c.xml"
    rep.write_text(f'<coverage><sources><source>{root / "src"}</source></sources><packages><package name="p"><classes>'
                   '<class name="c" filename="calc.py"><lines><line number="3" hits="1"/><line number="4" hits="0"/>'
                   '</lines></class></classes></package></packages></coverage>', encoding="utf-8")
    code, data = run(capsys, root, base, "coverage", "--report", str(rep))
    assert code == 1 and data["details"][0]["file"] == "src/calc.py"


def test_coverage_go_module_prefix_via_go_mod(tmp_path, capsys):
    root, base = init(tmp_path, {"go.mod": "module example.com/m\n\ngo 1.22\n", "pkg/calc.go": "package pkg\n\nfunc A() {}\n"})
    append(root, "pkg/calc.go", "func B() {\n\tx := 1\n\t_ = x\n}\n")
    rep = tmp_path / "g.out"
    rep.write_text("mode: set\nexample.com/m/pkg/calc.go:4.10,6.2 2 0\n", encoding="utf-8")
    code, data = run(capsys, root, base, "coverage", "--report", str(rep))
    assert code == 1 and data["details"][0]["file"] == "pkg/calc.go" and data["details"][0]["uncovered"] == "4-6"


def test_coverage_subproject_relative_path(tmp_path, capsys):
    root, base = cov_repo(tmp_path, "svc/src/calc.py")
    rep = write_report(tmp_path, "lcov", "src/calc.py", {1: 1, 2: 1, 3: 1, 4: 0, 5: 1})
    code, data = run(capsys, root, base, "coverage", "--report", str(rep))
    assert code == 1 and data["details"][0]["file"] == "svc/src/calc.py"


def test_coverage_ambiguous_path_is_3(tmp_path, capsys):
    root, base = init(tmp_path, {"a/utils.py": PY_BASE, "b/utils.py": PY_BASE})
    append(root, "a/utils.py", PY_ADDED)
    rep = write_report(tmp_path, "lcov", "utils.py", ALL_COVERED)
    code, data = run(capsys, root, base, "coverage", "--report", str(rep))
    assert code == 3 and "a/utils.py" in data["error"] and "b/utils.py" in data["error"]


@pytest.mark.parametrize("name,fmt", [("cobertura.xml", "cobertura"), ("lcov.info", "lcov"), ("go.cover", "go"),
                                      ("coveragepy.json", "coveragepy-json"), ("jacoco.xml", "jacoco")])
def test_coverage_fixture_parsers(name, fmt):
    data = (FIXTURES / name).read_bytes()
    assert dg.detect_coverage(data) == fmt
    entries = dg.COV_PARSERS[fmt](data)
    lines = {p[0]: ls for p, ls in entries}
    first = next(iter(lines.values()))
    assert any(h == 0 for h in first.values()) and any(h > 0 for h in first.values())
    if fmt == "cobertura":
        assert dg.norm_path(entries[0][0][0]) == "C:/work/repo/src/Calc/Adder.cs" and entries[0][1] == {5: 3, 6: 0}
    if fmt == "go":
        assert first[8] == 0 and first[3] == 1 and first[4] == 1
    if fmt == "coveragepy-json":
        assert 9 not in first
    if fmt == "jacoco":
        assert entries[0][0] == ["com/acme/Calc.java"]


# ---- duplication

def dup_repo(tmp_path: Path, extra=None):
    return init(tmp_path, {"src/a.py": "def one():\n" + block("a", 8, "    ") + "    return 1\n",
                           **(extra or {})})


def test_duplication_copy_paste_caught_with_both_locations(tmp_path, capsys):
    root, base = dup_repo(tmp_path)
    write(root, "src/b.py", "def two():\n" + block("a", 8, "    ") + "    return 2\n")
    code, data = run(capsys, root, base, "duplication")
    assert code == 1 and data["value"] == 1
    clone = data["details"][0]["clone"]
    assert "src/a.py:2-9" in clone and "src/b.py:2-9" in clone and "(8 lines)" in clone


def test_duplication_within_added_code(tmp_path, capsys):
    root, base = init(tmp_path, {"README.md": "x\n"})
    write(root, "src/c.py", block("z", 7) + "other = 1\n" + block("z", 7))
    code, data = run(capsys, root, base, "duplication")
    assert code == 1 and data["value"] == 1
    assert "src/c.py:1-7" in data["details"][0]["clone"] and "src/c.py:9-15" in data["details"][0]["clone"]


def test_duplication_moved_code_is_not_a_violation(tmp_path, capsys):
    root, base = dup_repo(tmp_path)
    write(root, "src/a.py", "def one():\n    return 1\n")
    write(root, "src/b.py", "def two():\n" + block("a", 8, "    ") + "    return 2\n")
    code, data = run(capsys, root, base, "duplication")
    assert code == 0 and data["value"] == 0


def test_duplication_trivial_lines_not_flagged(tmp_path, capsys):
    trivial = "}\n{\nend\nimport os\nusing System;\n});\n}\n{\nend\n"
    root, base = init(tmp_path, {"src/a.js": trivial})
    write(root, "src/b.js", trivial)
    code, _ = run(capsys, root, base, "duplication")
    assert code == 0


def test_duplication_min_lines_and_max_new(tmp_path, capsys):
    root, base = dup_repo(tmp_path)
    write(root, "src/b.py", "".join(block("a", 5)))
    assert run(capsys, root, base, "duplication")[0] == 0
    assert run(capsys, root, base, "duplication", "--min-lines", "5")[0] == 1
    assert run(capsys, root, base, "duplication", "--min-lines", "5", "--max-new", "1")[0] == 0


def test_duplication_excludes_honoured(tmp_path, capsys):
    root, base = dup_repo(tmp_path, {"vendor/lib.py": block("v")})
    write(root, "src/b.py", block("v"))
    assert run(capsys, root, base, "duplication")[0] == 0
    write(root, "src/c.py", block("a"))
    assert run(capsys, root, base, "duplication")[0] == 1
    assert run(capsys, root, base, "duplication", "--exclude", "src/c.py")[0] == 0
    assert run(capsys, root, base, "duplication", "--exclude", "src/a.py", "--exclude", "src/c.py")[0] == 0


def test_duplication_skips_clones_inside_frozen_acceptance_tests(tmp_path, capsys):
    root, base = dup_repo(tmp_path)
    write(root, "tests/test_x.py", block("t", 7) + "other = 1\n" + block("t", 7))
    assert run(capsys, root, base, "duplication")[0] == 1
    write(root, ".forge/frozen.json", '{"files": {"tests/test_x.py": "sha"}}')
    assert run(capsys, root, base, "duplication")[0] == 0
    write(root, "src/b.py", block("t", 7))
    assert run(capsys, root, base, "duplication")[0] == 1


def test_duplication_old_duplicates_untouched_pass(tmp_path, capsys):
    root, base = dup_repo(tmp_path, {"src/old.py": block("a")})
    write(root, "docs.md", "hello\n")
    assert run(capsys, root, base, "duplication")[0] == 0


def test_duplication_fast_on_many_files(tmp_path, capsys):
    files = {f"src/m{i}/f{i}.py": block(f"f{i}", 20) for i in range(600)}
    root, base = init(tmp_path, files)
    write(root, "src/new.py", block("f7", 20))
    code, data = run(capsys, root, base, "duplication")
    assert code == 1 and "src/m7/f7.py" in data["details"][0]["clone"]


# ---- complexity

CX_REPO = {"src/calc.py": "".join(f"x{i} = {i}\n" for i in range(1, 41)),
           "src/calc.ts": "".join(f"const x{i} = {i};\n" for i in range(1, 41)),
           "pkg/calc.go": "package pkg\n" + "".join(f"var x{i} = {i}\n" for i in range(1, 41))}
CX_FIXTURES = [("lizard.csv", "lizard-csv", "src/calc.py"), ("lizard.xml", "lizard-xml", "src/calc.py"),
               ("eslint.json", "eslint-json", "src/calc.ts"), ("radon.json", "radon-json", "src/calc.py"),
               ("sarif.json", "sarif", "src/calc.ts")]


@pytest.mark.parametrize("name,fmt,rel", CX_FIXTURES + [("gocyclo.txt", "gocyclo", "pkg/calc.go")])
def test_complexity_fixture_parsers(name, fmt, rel):
    data = (FIXTURES / name).read_bytes()
    assert dg.detect_complexity(data) == fmt
    route = [f for f in dg.CX_PARSERS[fmt](data) if "route" in f["name"]]
    assert len(route) == 1 and route[0]["ccn"] == 18 and route[0]["start"] == 10
    assert route[0]["end"] == (None if fmt == "gocyclo" else 31)


@pytest.mark.parametrize("name,fmt,rel", CX_FIXTURES)
def test_complexity_touched_offender_fails_untouched_passes(tmp_path, capsys, name, fmt, rel):
    root, base = init(tmp_path, CX_REPO)
    report = str(FIXTURES / name)
    edit_line(root, rel, 3, "changed = 1")
    code, data = run(capsys, root, base, "complexity", "--report", report, "--format", fmt)
    assert code == 0 and data["ok"]
    edit_line(root, rel, 15, "changed = 2")
    code, data = run(capsys, root, base, "complexity", "--report", report)
    assert code == 1 and data["value"] == 18 and data["threshold"] == 15
    row = data["details"][0]
    assert row["file"] == rel and "route" in row["function"] and row["ccn"] == 18 and row["kind"] == "violation"
    assert run(capsys, root, base, "complexity", "--report", report, "--max", "20")[0] == 0


def test_complexity_gocyclo_uses_start_line_and_says_so(tmp_path, capsys):
    root, base = init(tmp_path, CX_REPO)
    report = str(FIXTURES / "gocyclo.txt")
    edit_line(root, "pkg/calc.go", 15, "var changed = 1")
    assert run(capsys, root, base, "complexity", "--report", report)[0] == 0
    edit_line(root, "pkg/calc.go", 10, "var changed = 2")
    code, data = run(capsys, root, base, "complexity", "--report", report)
    assert code == 1 and "start line" in data["details"][0]["note"]


def test_complexity_base_report_pre_existing_not_worse_passes(tmp_path, capsys):
    root, base = init(tmp_path, CX_REPO)
    edit_line(root, "src/calc.py", 15, "changed = 1")
    row = '20,{ccn},120,2,22,"x","./src/calc.py","route","route( a )",10,31\n'
    cur = tmp_path / "cur.csv"
    cur.write_text(row.format(ccn=18), encoding="utf-8")
    for ccn, expected in ((20, 0), (18, 0), (17, 1), (10, 1)):
        old = tmp_path / "old.csv"
        old.write_text(row.format(ccn=ccn), encoding="utf-8")
        code, data = run(capsys, root, base, "complexity", "--report", str(cur), "--base-report", str(old))
        assert code == expected, ccn
        if expected == 0:
            assert data["details"][0]["kind"] == "pre-existing"
    assert run(capsys, root, base, "complexity", "--report", str(cur))[0] == 1


def test_complexity_missing_or_unknown_report_is_3(tmp_path, capsys):
    root, base = init(tmp_path, CX_REPO)
    assert run(capsys, root, base, "complexity", "--report", str(tmp_path / "none.csv"))[0] == 3
    junk = tmp_path / "junk.txt"
    junk.write_text("nothing here\n", encoding="utf-8")
    assert run(capsys, root, base, "complexity", "--report", str(junk))[0] == 3


# ---- size

def size_repo(tmp_path: Path):
    root, base = init(tmp_path, {"src/a.py": "x = 1\n", "README.md": "r\n"})
    write(root, "src/big.py", block("b", 30))
    write(root, "tests/test_big.py", block("t", 100))
    write(root, "package-lock.json", "{\n" + "\n" * 500 + "}\n")
    return root, base


def test_size_over_budget_fails_tests_and_locks_excluded(tmp_path, capsys):
    root, base = size_repo(tmp_path)
    code, data = run(capsys, root, base, "size", "--max-lines", "40")
    assert code == 0 and data["value"] == 30
    code, data = run(capsys, root, base, "size", "--max-lines", "29")
    assert code == 1 and data["value"] == 30 and data["threshold"] == 29


def test_size_counts_modified_and_deleted_lines_and_file_budget(tmp_path, capsys):
    root, base = size_repo(tmp_path)
    write(root, "src/a.py", "x = 2\ny = 3\n")
    code, data = run(capsys, root, base, "size", "--max-lines", "100", "--max-files", "1")
    assert code == 1 and data["value"] == 33 and data["details"][0]["files"] == 2
    assert run(capsys, root, base, "size", "--max-files", "2")[0] == 0


# ---- all, profile, json shape

PROFILE = "diff_gates:\n  size:\n    max_lines: 50\n  duplication: {min_lines: 6, max_new: 0}\n  coverage:\n    report: cov.info\n    min: 80\n"


def all_repo(tmp_path: Path):
    root, base = init(tmp_path, {"powerpuff.yaml": PROFILE, "src/calc.py": PY_BASE})
    append(root, "src/calc.py", PY_ADDED)
    (root / "cov.info").write_text("SF:src/calc.py\n" + "".join(f"DA:{n},1\n" for n in range(1, 6)) + "end_of_record\n",
                                   encoding="utf-8")
    return root, base


def test_all_runs_profile_gates_and_writes_fragment(tmp_path, capsys):
    root, base = all_repo(tmp_path)
    code, data = run(capsys, root, base, "all", "--config", "powerpuff.yaml", "--out", "frag.json")
    assert code == 0 and data["ok"]
    frag = json.loads((root / "frag.json").read_text(encoding="utf-8"))
    assert frag["schema"] == 1 and {g["id"] for g in frag["gates"]} == {"size", "duplication", "diff-coverage"}
    assert all(g["status"] == "passed" and g["blocking"] for g in frag["gates"])
    assert next(g for g in frag["gates"] if g["id"] == "size")["threshold"] == 50


def test_all_failing_gate_is_1_and_unevaluable_gate_is_3(tmp_path, capsys):
    root, base = all_repo(tmp_path)
    write(root, "src/copy.py", PY_BASE + PY_ADDED + "".join(block("q", 7)))
    write(root, "src/copy2.py", "".join(block("q", 7)))
    code, data = run(capsys, root, base, "all", "--config", "powerpuff.yaml", "--out", "frag.json")
    assert code == 1
    frag = json.loads((root / "frag.json").read_text(encoding="utf-8"))
    assert next(g for g in frag["gates"] if g["id"] == "duplication")["status"] == "failed"
    (root / "cov.info").unlink()
    code, data = run(capsys, root, base, "all", "--config", "powerpuff.yaml", "--out", "frag.json")
    assert code == 3 and not data["ok"]
    frag = json.loads((root / "frag.json").read_text(encoding="utf-8"))
    assert "not found" in next(g for g in frag["gates"] if g["id"] == "diff-coverage")["error"]


def test_all_without_section_is_3(tmp_path, capsys):
    root, base = init(tmp_path, {"powerpuff.yaml": "stack: python\n", "a.py": "x = 1\n"})
    assert run(capsys, root, base, "all", "--config", "powerpuff.yaml")[0] == 3


def test_profile_section_fallback_parser():
    text = "stack: x\ndiff_gates:\n  coverage:\n    report: out/cov.info   # c\n    min: 75\n    exclude: [a/*, b/*]\n" \
           "  size: {max-lines: 10, max-files: 3}\ncommands:\n  build: make\n"
    assert dg.parse_section(text) == {"coverage": {"report": "out/cov.info", "min": 75, "exclude": ["a/*", "b/*"]},
                                      "size": {"max-lines": 10, "max-files": 3}}


def test_json_shape_and_human_output(tmp_path, capsys):
    root, base = cov_repo(tmp_path)
    rep = write_report(tmp_path, "lcov", "src/calc.py", ALL_COVERED)
    _, data = run(capsys, root, base, "coverage", "--report", str(rep))
    assert list(data) == ["gate", "ok", "value", "threshold", "details"]
    assert isinstance(data["details"], list)
    capsys.readouterr()
    code = dg.main(["coverage", "--base", base, "--root", str(root), "--report", str(rep)])
    out = capsys.readouterr().out
    assert code == 0 and out.startswith("coverage: PASS")
