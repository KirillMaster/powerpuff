"""profile_init: stack detection, tool/config filtering, never-overwrite, unknown stack, presets load via the loader."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import forge_run  # noqa: E402
import profile_init  # noqa: E402

STACKS = ["dotnet", "typescript", "python", "rust", "go"]


def put(root: Path, rel: str, text: str = "") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def tools(monkeypatch, *names: str, everything: bool = False) -> None:
    monkeypatch.setattr(profile_init.shutil, "which",
                        lambda name, path=None: f"/bin/{name}" if everything or name in names else None)


def init(root: Path, capsys, *args: str) -> tuple[int, str, str]:
    rc = profile_init.main(["--repo", str(root), *args])
    out = capsys.readouterr()
    return rc, out.out, out.err


def profile_of(text: str, tmp_path: Path) -> dict:
    d = tmp_path / "parsed"
    d.mkdir(exist_ok=True)
    (d / "powerpuff.yaml").write_text(text, encoding="utf-8")
    prof = forge_run.load_profile(d)
    assert prof is not None
    return prof


def test_presets_are_complete():
    presets = profile_init.load_presets()
    assert sorted(presets) == sorted(STACKS)
    for stack, cfg in presets.items():
        ids = [g for t in profile_init.TIERS for g in cfg["gates"].get(t, [])]
        assert set(ids) <= set(cfg["commands"]), stack
        assert set(cfg["commands"]) == set(ids), stack
        assert set(cfg["needs"]) - {"format_fix", "setup"} <= set(ids), stack
        assert set(cfg["test_gates"]) <= set(ids), stack
        assert cfg["detect"] and cfg["protected"] and cfg["format_fix"], stack
        assert all("{plugin}" not in c or '"{plugin}/scripts/diff_gates.py"' in c for c in cfg["commands"].values())


def test_presets_parse_identically_without_pyyaml(monkeypatch):
    with_yaml = profile_init.load_presets()
    monkeypatch.setitem(sys.modules, "yaml", None)
    assert profile_init.load_presets() == with_yaml


def test_dotnet_detection_and_dropped_commands(tmp_path, monkeypatch, capsys):
    put(tmp_path, "App.sln")
    put(tmp_path, "tests/App.Tests/App.Tests.csproj", "<Project/>")
    put(tmp_path, "tests/App.Tests/FooTests.cs", "class FooTests {}")
    tools(monkeypatch, "dotnet", "python")
    rc, out, _ = init(tmp_path, capsys)
    prof = profile_of(out, tmp_path)
    assert rc == 0 and prof["approved"] is False
    assert prof["gates"]["build"] == "dotnet build -warnaserror"
    assert prof["gates"]["format"] == "dotnet format --verify-no-changes"
    assert prof["gates"]["tests"] == "dotnet test"
    assert "coverage" not in prof["gates"] and "diff-coverage" not in prof["gates"]
    assert "#   coverage: dotnet test" in out and "tool:lizard" in out and "tool:qg" in out
    assert "approved: false" in out and "approved: true" in out
    assert prof["config"]["test_gates"] == ["tests"]


def test_dotnet_coverage_and_qg_kept_when_available(tmp_path, monkeypatch, capsys):
    put(tmp_path, "App.sln")
    put(tmp_path, "App.Tests/App.Tests.csproj", '<PackageReference Include="coverlet.collector" />')
    tools(monkeypatch, "dotnet", "python", "qg")
    _, out, _ = init(tmp_path, capsys)
    prof = profile_of(out, tmp_path)
    assert {"coverage", "diff-coverage", "qg-pr", "qg-mutation"} <= set(prof["gates"]) | set(prof["final_gates"])
    assert prof["final_gates"]["qg-mutation"] == "qg mutation-diff"
    assert '"{plugin}/scripts/diff_gates.py"' in prof["gates"]["diff-coverage"]


def test_plugin_path_override(tmp_path, monkeypatch, capsys):
    put(tmp_path, "go.mod", "module x")
    put(tmp_path, "a_test.go", "package x")
    tools(monkeypatch, "go", "gofmt", "python")
    _, out, _ = init(tmp_path, capsys, "--plugin", "D:\\some dir\\pp")
    assert '"D:/some dir/pp/scripts/diff_gates.py"' in profile_of(out, tmp_path)["gates"]["duplication"]


def test_typescript_uses_package_scripts_and_lockfile(tmp_path, monkeypatch, capsys):
    pkg = {"scripts": {"lint": "eslint .", "test": "vitest run", "build": "tsc"},
           "devDependencies": {"typescript": "5", "prettier": "3", "vitest": "2", "@vitest/coverage-v8": "2"}}
    put(tmp_path, "package.json", json.dumps(pkg))
    put(tmp_path, "tsconfig.json", "{}")
    put(tmp_path, "pnpm-lock.yaml")
    tools(monkeypatch, "python")
    _, out, _ = init(tmp_path, capsys)
    prof = profile_of(out, tmp_path)
    assert prof["gates"]["lint"] == "pnpm run lint"
    assert prof["gates"]["typecheck"] == "pnpm exec tsc --noEmit"
    assert prof["gates"]["format"] == "pnpm exec prettier --check ."
    assert prof["gates"]["coverage-vitest"].startswith("pnpm exec vitest run --coverage")
    assert "lint-eslint" not in prof["gates"] and "coverage-jest" not in prof["gates"]
    assert prof["format_fix"] == "pnpm exec prettier --write ."
    assert prof["config"]["setup"] == "pnpm install"
    assert "mutation" in out and "#   mutation:" in out


def test_typescript_default_npm_test_script_is_not_a_test(tmp_path, monkeypatch, capsys):
    pkg = {"scripts": {"test": 'echo "Error: no test specified" && exit 1'}, "devDependencies": {"typescript": "5"}}
    put(tmp_path, "package.json", json.dumps(pkg))
    put(tmp_path, "tsconfig.json", "{}")
    tools(monkeypatch, "python")
    _, out, _ = init(tmp_path, capsys)
    prof = profile_of(out, tmp_path)
    assert "tests" not in prof["gates"] and prof["gates"]["typecheck"].startswith("npx --no-install tsc")
    assert prof["config"].get("test_gates") is None


def test_python_keeps_only_configured_tools(tmp_path, monkeypatch, capsys):
    put(tmp_path, "pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    put(tmp_path, "tests/test_a.py", "def test_a(): pass\n")
    tools(monkeypatch, "pytest", "python", "mypy")
    _, out, _ = init(tmp_path, capsys)
    prof = profile_of(out, tmp_path)
    assert prof["gates"]["lint"] == "ruff check ." and prof["gates"]["format"] == "ruff format --check ."
    assert prof["gates"]["tests"] == "python -m pytest -q"
    assert "typecheck" not in prof["gates"] and "typecheck-mypy" not in prof["gates"]
    assert "#   typecheck: pyright" in out and "#   coverage: python -m coverage" in out
    assert prof["format_fix"] == "ruff format ."


def test_python_pytest_needs_tests(tmp_path, monkeypatch, capsys):
    put(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    tools(monkeypatch, "pytest", "python")
    _, out, _ = init(tmp_path, capsys)
    prof = profile_of(out, tmp_path)
    assert "tests" not in prof["gates"] and "#   tests: python -m pytest" in out
    assert {"duplication", "size"} <= set(prof["gates"])


def test_rust_and_go(tmp_path, monkeypatch, capsys):
    rs, go = tmp_path / "rs", tmp_path / "go"
    put(rs, "Cargo.toml", "[package]\nname='x'\n")
    put(rs, "src/lib.rs", "#[test]\nfn t() {}\n")
    put(go, "go.mod", "module x\n")
    put(go, "x_test.go", "package x\n")
    tools(monkeypatch, "cargo", "cargo-fmt", "cargo-clippy", "go", "gofmt", "python")
    prof_rs = profile_of(init(rs, capsys)[1], tmp_path)
    assert prof_rs["gates"]["lint"] == "cargo clippy --all-targets -- -D warnings"
    assert prof_rs["gates"]["tests"] == "cargo test" and "coverage" not in prof_rs["gates"]
    prof_go = profile_of(init(go, capsys)[1], tmp_path)
    assert prof_go["gates"]["vet"] == "go vet ./..." and prof_go["gates"]["tests"] == "go test ./..."
    assert "lint" not in prof_go["gates"]
    assert prof_go["final_gates"]["tests-full"] == "go test -race ./..."


def test_monorepo_prefixes_and_cd_scoped_commands(tmp_path, monkeypatch, capsys):
    put(tmp_path, "web/package.json", json.dumps({"scripts": {"test": "vitest"}, "devDependencies": {"typescript": "5"}}))
    put(tmp_path, "web/tsconfig.json", "{}")
    put(tmp_path, "svc/go.mod", "module x\n")
    put(tmp_path, "svc/a_test.go", "package x\n")
    tools(monkeypatch, "go", "gofmt", "python")
    rc, out, _ = init(tmp_path, capsys)
    prof = profile_of(out, tmp_path)
    assert rc == 0 and "stack: mixed" in out
    assert prof["gates"]["svc-tests"] == "cd svc && go test ./..."
    assert prof["gates"]["web-typecheck"] == "cd web && npx --no-install tsc --noEmit"
    assert prof["gates"]["web-tests"] == "cd web && npm run test"
    assert [g for g in prof["gates"] if g.endswith("duplication") or g.endswith("size")] == ["duplication", "size"]
    assert "cd " not in prof["gates"]["duplication"]
    assert prof["format_fix"] == "cd svc && gofmt -w ."
    assert "svc/.golangci.yml" in out and "web/tsconfig*.json" in out


def test_monorepo_subproject_diff_gate_gets_root_and_ignores_unreported_files(tmp_path, monkeypatch, capsys):
    put(tmp_path, "svc/go.mod", "module x\n")
    put(tmp_path, "svc/a_test.go", "package x\n")
    put(tmp_path, "tool/Cargo.toml", "[package]\nname='x'\n")
    put(tmp_path, "tool/src/lib.rs", "#[test]\nfn t() {}\n")
    tools(monkeypatch, everything=True)
    _, out, _ = init(tmp_path, capsys)
    cmd = profile_of(out, tmp_path)["gates"]["svc-diff-coverage"]
    assert cmd.startswith("cd svc && python") and "--root {root}" in cmd and cmd.endswith("--missing-file ignore")


def test_never_overwrites_existing_profile(tmp_path, monkeypatch, capsys):
    put(tmp_path, "go.mod", "module x\n")
    put(tmp_path, "powerpuff.yaml", "approved: true\n")
    tools(monkeypatch, "go", "gofmt", "python")
    rc, out, err = init(tmp_path, capsys, "--write")
    assert rc == 1 and "not overwritten" in err and out.startswith("# powerpuff.yaml")
    assert (tmp_path / "powerpuff.yaml").read_text(encoding="utf-8") == "approved: true\n"


def test_never_overwrites_json_profile(tmp_path, monkeypatch, capsys):
    put(tmp_path, "go.mod", "module x\n")
    put(tmp_path, "powerpuff.gates.json", "{}")
    tools(monkeypatch, "go", "gofmt", "python")
    assert init(tmp_path, capsys, "--write")[0] == 1
    assert not (tmp_path / "powerpuff.yaml").exists()


def test_write_creates_unapproved_profile(tmp_path, monkeypatch, capsys):
    put(tmp_path, "go.mod", "module x\n")
    tools(monkeypatch, "go", "gofmt", "python")
    rc, out, _ = init(tmp_path, capsys, "--write")
    written = (tmp_path / "powerpuff.yaml").read_text(encoding="utf-8")
    assert rc == 0 and written == out
    assert forge_run.load_profile(tmp_path)["approved"] is False


def test_without_write_nothing_is_created(tmp_path, monkeypatch, capsys):
    put(tmp_path, "go.mod", "module x\n")
    tools(monkeypatch, "go", "gofmt", "python")
    init(tmp_path, capsys)
    assert not (tmp_path / "powerpuff.yaml").exists()


def test_unknown_stack_gives_skeleton_and_exit_2(tmp_path, capsys):
    put(tmp_path, "Makefile", "all:\n")
    put(tmp_path, "run.sh", "echo hi\n")
    rc, out, err = init(tmp_path, capsys)
    assert rc == 2 and "commands: {}" in out and "approved: false" in out and "TODO" not in out
    assert "no preset - the profile is written by hand" in err and "docs/profile.md" in err
    assert not (tmp_path / "powerpuff.yaml").exists()


def test_unknown_stack_write_still_exit_2(tmp_path, capsys):
    put(tmp_path, "Makefile", "all:\n")
    rc, out, _ = init(tmp_path, capsys, "--write")
    assert rc == 2 and (tmp_path / "powerpuff.yaml").read_text(encoding="utf-8") == out


def test_stack_option_overrides_detection(tmp_path, monkeypatch, capsys):
    put(tmp_path, "Makefile", "all:\n")
    tools(monkeypatch, "go", "gofmt", "python")
    rc, out, _ = init(tmp_path, capsys, "--stack", "go")
    assert rc == 0 and "stack: go" in out
    assert init(tmp_path, capsys, "--stack", "cobol")[0] == 2


@pytest.mark.parametrize("pyyaml", [True, False])
@pytest.mark.parametrize("stack", STACKS)
def test_every_preset_loads_through_the_profile_loader(tmp_path, monkeypatch, capsys, stack, pyyaml):
    tools(monkeypatch, everything=True)
    if not pyyaml:
        monkeypatch.setitem(sys.modules, "yaml", None)
    put(tmp_path, "pkg/package.json", json.dumps({"scripts": {"lint": "x", "test": "y", "build": "z"},
                                                  "devDependencies": {"typescript": "5", "prettier": "3"}}))
    rc, out, _ = init(tmp_path, capsys, "--stack", stack)
    prof = profile_of(out, tmp_path)
    assert rc == 0 and prof["approved"] is False
    assert prof["gates"] and all(isinstance(c, str) and c for c in prof["gates"].values())
    for name in prof["config"].get("test_gates", []):
        assert name in prof["gates"]
    assert "{pm" not in out
