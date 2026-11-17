import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import gate_guard  # noqa: E402

BASE_FILES = {
    "src/app.ts": "export const a = 1;\n",
    "src/Foo.cs": "class Foo { }\n",
    "src/util.py": "def f():\n    return 1\n",
    "tests/app.test.ts": "it('a', () => {});\nit('b', () => {});\n",
    "tests/FooTests.cs": "[Fact]\npublic void A() { }\n[Fact]\npublic void B() { }\n",
    "tests/test_util.py": "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n",
    "pyproject.toml": "[tool.ruff]\nline-length = 100\n",
}


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


def commit(root: Path, msg: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "--no-gpg-sign", "-q", "-m", msg)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    for rel, text in BASE_FILES.items():
        write(tmp_path, rel, text)
    return tmp_path, commit(tmp_path, "base")


def rules(report: dict) -> set[str]:
    return {v["rule"] for v in report["violations"]}


@pytest.mark.parametrize("rel,line", [
    ("tests/FooTests.cs", '[Fact(Skip = "later")]\npublic void C() { }\n'),
    ("tests/FooTests.cs", "[Ignore]\npublic void C() { }\n"),
    ("tests/app.test.ts", "it.skip('c', () => {});\n"),
    ("tests/app.test.ts", "xit('c', () => {});\n"),
    ("tests/test_util.py", "\n\n@pytest.mark.skip\ndef test_c():\n    pass\n"),
    ("tests/test_util.py", "\n\n@pytest.mark.xfail\ndef test_c():\n    pass\n"),
])
def test_skip_added(repo, rel, line):
    root, base = repo
    append(root, rel, line)
    report = gate_guard.check(root, base)
    assert not report["ok"]
    assert "skip-added" in rules(report)


@pytest.mark.parametrize("rel,line", [
    ("src/Foo.cs", "#pragma warning disable CS0168\n"),
    ("src/Foo.cs", '[SuppressMessage("a", "b")]\n'),
    ("src/app.ts", "// @ts-ignore\n"),
    ("src/app.ts", "// eslint-disable-next-line no-console\n"),
    ("src/util.py", "x = 1  # noqa\n"),
    ("src/util.py", "y: int = 'a'  # type: ignore\n"),
])
def test_suppression_added(repo, rel, line):
    root, base = repo
    append(root, rel, line)
    report = gate_guard.check(root, base)
    assert rules(report) == {"suppression-added"}
    assert report["violations"][0]["file"] == rel


def test_committed_tamper_is_caught(repo):
    root, base = repo
    append(root, "src/util.py", "x = 1  # noqa\n")
    commit(root, "tamper")
    assert "suppression-added" in rules(gate_guard.check(root, base))


def test_line_number_reported(repo):
    root, base = repo
    append(root, "src/util.py", "x = 1  # noqa\n")
    v = gate_guard.check(root, base)["violations"][0]
    assert v["line"] == 3 and "noqa" in v["text"]


def test_protected_modified(repo):
    root, base = repo
    append(root, "pyproject.toml", "select = []\n")
    report = gate_guard.check(root, base)
    assert rules(report) == {"protected-changed"}
    assert report["violations"][0]["file"] == "pyproject.toml"


def test_new_protected_file_in_subfolder(repo):
    root, base = repo
    write(root, "web/.eslintrc.json", "{}\n")
    report = gate_guard.check(root, base)
    assert rules(report) == {"protected-changed"}
    assert report["violations"][0]["file"] == "web/.eslintrc.json"


def test_protected_dir_glob_and_unprotected(repo):
    root, base = repo
    write(root, "quality/baseline.json", "{}\n")
    assert "protected-changed" in rules(gate_guard.check(root, base))
    assert gate_guard.check(root, base, {"unprotected": ["quality/**"]})["ok"]


def test_config_protected_adds_glob(repo):
    root, base = repo
    write(root, "docs/frozen.md", "x\n")
    assert gate_guard.check(root, base)["ok"]
    assert not gate_guard.check(root, base, {"protected": ["docs/frozen.md"]})["ok"]


def test_test_file_deleted(repo):
    root, base = repo
    git(root, "rm", "-q", "tests/test_util.py")
    report = gate_guard.check(root, base)
    assert {"test-deleted", "test-count-decreased"} <= rules(report)


def test_test_count_decreased_other_file_untouched(repo):
    root, base = repo
    write(root, "tests/test_util.py", "def test_a():\n    assert True\n")
    report = gate_guard.check(root, base)
    assert rules(report) == {"test-count-decreased"}
    assert report["tests"] == {"before": 6, "after": 5}


def test_untracked_file_suppression(repo):
    root, base = repo
    write(root, "src/new.ts", "const a = 1;\n// @ts-nocheck\n")
    report = gate_guard.check(root, base)
    assert rules(report) == {"suppression-added"}
    assert (report["violations"][0]["file"], report["violations"][0]["line"]) == ("src/new.ts", 2)


def test_test_renamed_to_non_test_path(repo):
    root, base = repo
    git(root, "mv", "tests/test_util.py", "src/helper.py")
    report = gate_guard.check(root, base)
    assert "test-deleted" in rules(report)
    assert "renamed to src/helper.py" in [v for v in report["violations"] if v["rule"] == "test-deleted"][0]["text"]


def test_normal_code_and_test_addition_ok(repo):
    root, base = repo
    append(root, "src/util.py", "\n\ndef g():\n    return 2\n")
    append(root, "tests/test_util.py", "\n\ndef test_g():\n    assert True\n")
    report = gate_guard.check(root, base)
    assert report["ok"] and report["tests"] == {"before": 6, "after": 7}
    assert report["counts"] == {}


def test_skip_word_in_production_code_ok(repo):
    root, base = repo
    write(root, "src/list.js", "const r = items.skip(1);\n")
    append(root, "src/app.ts", "const s = xs.skip(2);\n")
    assert gate_guard.check(root, base)["ok"]


def test_allow_listed_suppression(repo):
    root, base = repo
    append(root, "src/util.py", "x = 1  # noqa: E501\n")
    cfg = {"allow_suppressions": [{"rule": "suppression-added", "file": "src/*.py", "pattern": "E501"}]}
    report = gate_guard.check(root, base, cfg)
    assert report["ok"] and len(report["allowed"]) == 1
    cfg["allow_suppressions"][0]["pattern"] = "F401"
    assert not gate_guard.check(root, base, cfg)["ok"]


def test_diff_only_adds_tests_ok(repo):
    root, base = repo
    write(root, "tests/test_more.py", "def test_x():\n    assert 1\n\n\ndef test_y():\n    assert 2\n")
    report = gate_guard.check(root, base)
    assert report["ok"] and report["tests"] == {"before": 6, "after": 8}


def test_base_equals_head_clean_tree_ok(repo):
    root, base = repo
    report = gate_guard.check(root, base)
    assert report == {"ok": True, "violations": [], "allowed": [], "counts": {}, "tests": {"before": 6, "after": 6},
                      "unknown_extensions": []}


def test_guard_tooling_files_not_flagged(repo):
    root, base = repo
    write(root, "scripts/gate_guard.py", "P = ['# noqa', '@ts-ignore']\n")
    assert gate_guard.check(root, base)["ok"]


def test_simple_yaml_parser():
    text = (
        "name: x\n"
        "protected:\n"
        "  - docs/a.md\n"
        "  - \"b/**\"\n"
        "allow_suppressions:\n"
        "  - {rule: suppression-added, file: src/*.py, pattern: \"# noqa\"}\n"
        "  - rule: skip-added\n"
        "    file: tests/*\n"
    )
    cfg = gate_guard.parse_simple_yaml(text)
    assert cfg["protected"] == ["docs/a.md", "b/**"]
    assert cfg["allow_suppressions"] == [
        {"rule": "suppression-added", "file": "src/*.py", "pattern": "# noqa"},
        {"rule": "skip-added", "file": "tests/*"},
    ]


PROFILE = (
    "approved: true            # set by the human\n"
    "gates:\n"
    "  iteration: [build, lint, tests-fast]\n"
    "  unit:\n"
    "    - acceptance\n"
    "    - \"diff-coverage\"\n"
    "  final: []\n"
    "commands:\n"
    "  build: \"cargo build\"\n"
    "  lint: 'cargo clippy -- -D warnings'\n"
    "  acceptance: python -c \"import app; assert app.foo() == 42\"\n"
    "format_fix: \"cargo fmt\"\n"
    "protected: [Cargo.toml, \"ci/**\"]\n"
    "allow_suppressions:\n"
    "  - {rule: suppression-added, file: src/*.rs}\n"
)


def test_simple_yaml_parser_profile_shape():
    cfg = gate_guard.parse_simple_yaml(PROFILE)
    assert cfg["approved"] == "true"
    assert cfg["gates"] == {"iteration": ["build", "lint", "tests-fast"], "unit": ["acceptance", "diff-coverage"],
                            "final": []}
    assert cfg["commands"] == {"build": "cargo build", "lint": "cargo clippy -- -D warnings",
                               "acceptance": 'python -c "import app; assert app.foo() == 42"'}
    assert cfg["format_fix"] == "cargo fmt"
    assert cfg["protected"] == ["Cargo.toml", "ci/**"]
    assert cfg["allow_suppressions"] == [{"rule": "suppression-added", "file": "src/*.rs"}]


def test_simple_yaml_parser_matches_pyyaml_on_profile():
    yaml = pytest.importorskip("yaml")
    real, mine = yaml.safe_load(PROFILE), gate_guard.parse_simple_yaml(PROFILE)
    assert real["approved"] is True and mine["approved"] == "true"
    assert {k: v for k, v in real.items() if k != "approved"} == {k: v for k, v in mine.items() if k != "approved"}


def test_load_config_from_powerpuff_yaml(repo):
    root, base = repo
    write(root, "powerpuff.yaml", "allow_suppressions:\n  - {rule: suppression-added, file: src/*.py}\n")
    cfg = gate_guard.load_config(root, None)
    assert cfg["allow_suppressions"][0]["rule"] == "suppression-added"


@pytest.fixture
def poly(repo):
    root, _ = repo
    write(root, "src/lib.rs", "pub fn a() {}\n\n#[test]\nfn t1() {}\n\n#[tokio::test]\nasync fn t2() {}\n")
    write(root, "pkg/calc.go", "package pkg\n")
    write(root, "pkg/calc_test.go", "package pkg\n\nfunc TestA(t *testing.T) {}\n")
    write(root, "app/src/test/java/FooTest.java", "class FooTest {\n    @Test\n    void a() {}\n}\n")
    return root, commit(root, "poly")


@pytest.mark.parametrize("rel,line,rule", [
    ("src/lib.rs", "#[ignore]\nfn t3() {}\n", "skip-added"),
    ("src/lib.rs", "#[allow(dead_code)]\nfn unused() {}\n", "suppression-added"),
    ("pkg/calc_test.go", "func TestB(t *testing.T) {\n\tt.Skip(\"later\")\n}\n", "skip-added"),
    ("pkg/calc.go", "var x = 1 //nolint\n", "suppression-added"),
    ("app/src/test/java/FooTest.java", "@Disabled\n@Test\nvoid b() {}\n", "skip-added"),
    ("app/src/test/java/FooTest.java", "@SuppressWarnings(\"all\")\n", "suppression-added"),
])
def test_other_languages(poly, rel, line, rule):
    root, base = poly
    append(root, rel, line)
    assert rule in rules(gate_guard.check(root, base))


def test_rust_inline_test_removed(poly):
    root, base = poly
    write(root, "src/lib.rs", "pub fn a() {}\n\n#[test]\nfn t1() {}\n")
    report = gate_guard.check(root, base)
    assert rules(report) == {"test-count-decreased"}
    assert report["tests"] == {"before": 10, "after": 9}


def test_rust_protected_but_cargo_toml_not(poly):
    root, base = poly
    write(root, "Cargo.toml", "[package]\nname = \"x\"\n")
    assert gate_guard.check(root, base)["ok"]
    write(root, "clippy.toml", "msrv = \"1.70\"\n")
    assert rules(gate_guard.check(root, base)) == {"protected-changed"}


def test_language_defined_only_in_config(repo):
    root, base = repo
    write(root, "test/a_test.exs", "defmodule ATest do\n  @tag :skip\n  test \"x\" do\n  end\nend\n")
    assert gate_guard.check(root, base)["ok"]
    cfg = {"languages": {"elixir": {"ext": [".ex", ".exs"], "test_file": [r"_test\.exs$"],
                                    "skip": [r"@tag\s+:skip"], "test_decl": [r"^\s*test\s+\""]}}}
    report = gate_guard.check(root, base, cfg)
    assert rules(report) == {"skip-added"}
    assert report["unknown_extensions"] == []


def test_config_replaces_only_given_fields(repo):
    root, base = repo
    append(root, "src/util.py", "x = 1  # noqa\n")
    append(root, "tests/test_util.py", "\n\n@pytest.mark.skip\ndef test_c():\n    pass\n")
    cfg = {"languages": {"python": {"suppression": []}}}
    assert rules(gate_guard.check(root, base, cfg)) == {"skip-added"}


def test_languages_from_json_file(repo):
    root, base = repo
    write(root, "powerpuff.gates.json", json.dumps({"languages": {"ruby": {
        "ext": [".rb"], "test_file": ["_spec\\.rb$"], "skip": ["\\bxit\\b"]}}}))
    write(root, "spec/a_spec.rb", "xit 'later' do\nend\n")
    cfg = gate_guard.load_config(root, None)
    assert "skip-added" in rules(gate_guard.check(root, base, cfg))


def test_unknown_extensions_listed(repo):
    root, base = repo
    write(root, "lib/thing.rb", "# rubocop:disable all\nputs 1\n")
    report = gate_guard.check(root, base)
    assert report["ok"] and report["unknown_extensions"] == [".rb"]


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / "gate_guard.py"), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def test_cli_exit_codes(repo, tmp_path_factory):
    root, base = repo
    ok = run_cli("--base", base, "--root", str(root))
    assert ok.returncode == 0 and json.loads(ok.stdout)["ok"] is True

    append(root, "src/util.py", "x = 1  # noqa\n")
    bad = run_cli("--base", base, "--root", str(root))
    assert bad.returncode == 1
    assert json.loads(bad.stdout)["counts"] == {"suppression-added": 1}

    assert run_cli("--base", "deadbeef" * 5, "--root", str(root)).returncode == 3
    assert run_cli("--base", base, "--root", str(tmp_path_factory.mktemp("nogit"))).returncode == 3
