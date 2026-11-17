"""Attended-mode hooks: hooks/hooks.json, scripts/hook_stop.py, scripts/hook_protect.py (run as subprocesses)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STOP = ROOT / "scripts" / "hook_stop.py"
PROTECT = ROOT / "scripts" / "hook_protect.py"

GREEN = "import sys\nopen(r'{marker}', 'a').write('x')\nsys.exit(0)\n"
RED = "import sys\nopen(r'{marker}', 'a').write('x')\nprint('\\n'.join(f'line {{i}}' for i in range(100)))\nprint('boom')\nsys.exit(1)\n"


def profile(hooks: str = "  stop_gates: true\n  protect: true\n", gate: str = "python gate.py",
            protected: str = "[secrets/*]") -> str:
    text = f"approved: true\ngates:\n  iteration: [chk]\ncommands:\n  chk: {gate}\nprotected: {protected}\n"
    return text + (f"hooks:\n{hooks}" if hooks else "")


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args], cwd=repo, check=True,
                   capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    return r


def setup(repo: Path, prof: str | None, gate_body: str = GREEN) -> Path:
    marker = repo.parent / "marker"
    (repo / "gate.py").write_text(gate_body.format(marker=marker), encoding="utf-8")
    if prof is not None:
        (repo / "powerpuff.yaml").write_text(prof, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return marker


def run(script: Path, event, cwd: Path, **env):
    base = {k: v for k, v in os.environ.items() if not k.startswith("POWERPUFF_")}
    stdin = event if isinstance(event, str) else json.dumps({"session_id": "s1", "cwd": str(cwd), **event})
    return subprocess.run([sys.executable, str(script)], input=stdin, capture_output=True, text=True, timeout=60,
                          cwd=cwd, env={**base, **env}, encoding="utf-8")


def edit(repo: Path, rel: str, tool: str = "Edit"):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return run(PROTECT, {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {key: rel}}, repo)


def stop(repo: Path, **env):
    return run(STOP, {"hook_event_name": "Stop", "stop_hook_active": False}, repo, **env)


def dirty(repo: Path, text: str = "1") -> None:
    (repo / "new.txt").write_text(text, encoding="utf-8")


def runs(marker: Path) -> int:
    return len(marker.read_text()) if marker.is_file() else 0


def test_hooks_json_structure():
    data = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    assert data == {"hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": 'python "${CLAUDE_PLUGIN_ROOT}/scripts/hook_stop.py"',
                             "timeout": 600}]}],
        "PreToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [
            {"type": "command", "command": 'python "${CLAUDE_PLUGIN_ROOT}/scripts/hook_protect.py"', "timeout": 10}]}]}}
    assert STOP.is_file() and PROTECT.is_file()


@pytest.mark.parametrize("script", [STOP, PROTECT])
def test_no_profile_is_silent_noop(repo, script):
    setup(repo, None)
    dirty(repo)
    p = run(script, {"tool_name": "Edit", "tool_input": {"file_path": "secrets/a"}}, repo)
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")


@pytest.mark.parametrize("hooks", ["", "  stop_gates: false\n  protect: false\n"])
def test_switches_off_are_noop(repo, hooks):
    marker = setup(repo, profile(hooks))
    dirty(repo)
    assert edit(repo, "secrets/a.txt").returncode == 0
    p = stop(repo)
    assert (p.returncode, p.stderr) == (0, "") and runs(marker) == 0


def test_protect_blocks_glob_and_names_it(repo):
    setup(repo, profile())
    p = edit(repo, "secrets/a.txt", "Write")
    assert p.returncode == 2
    assert "secrets/a.txt" in p.stderr and "secrets/*" in p.stderr and "Ask the human" in p.stderr


def test_protect_blocks_profile_itself(repo):
    setup(repo, profile(protected="[]"))
    p = edit(repo, str(repo / "powerpuff.yaml"), "MultiEdit")
    assert p.returncode == 2 and "powerpuff.yaml" in p.stderr


def test_protect_allows_unprotected_and_outside_root(repo, tmp_path):
    setup(repo, profile())
    assert edit(repo, "src/app.py").returncode == 0
    assert edit(repo, str(tmp_path / "elsewhere" / "secrets" / "k.txt")).returncode == 0


def test_protect_notebook_path(repo):
    setup(repo, profile())
    assert edit(repo, "secrets/n.ipynb", "NotebookEdit").returncode == 2


@pytest.mark.skipif(sys.platform != "win32", reason="backslashes and case folding are Windows-only")
def test_protect_windows_backslash_and_case(repo):
    setup(repo, profile())
    assert edit(repo, "SECRETS\\Key.txt").returncode == 2
    assert edit(repo, str(repo).upper() + "\\Secrets\\Key.txt").returncode == 2


def test_protect_env_override_and_driver(repo):
    setup(repo, profile())
    event = {"tool_name": "Edit", "tool_input": {"file_path": "secrets/a.txt"}}
    assert run(PROTECT, event, repo, POWERPUFF_ALLOW_PROTECTED="1").returncode == 0
    assert run(PROTECT, event, repo, POWERPUFF_DRIVER="1").returncode == 2


@pytest.mark.parametrize("stdin", ["not json", "", "[1]"])
def test_protect_malformed_stdin_allows(repo, stdin):
    setup(repo, profile())
    assert run(PROTECT, stdin, repo).returncode == 0


def test_stop_clean_tree_does_not_run_gates(repo):
    marker = setup(repo, profile())
    p = stop(repo)
    assert p.returncode == 0 and runs(marker) == 0


def test_stop_green_allows(repo):
    marker = setup(repo, profile())
    dirty(repo)
    p = stop(repo)
    assert (p.returncode, p.stderr) == (0, "") and runs(marker) == 1


def test_stop_red_blocks_with_tail(repo):
    setup(repo, profile(), RED)
    dirty(repo)
    p = stop(repo)
    assert p.returncode == 2
    assert "`chk`" in p.stderr and "boom" in p.stderr and "do not disable or skip" in p.stderr
    assert "line 61\n" in p.stderr and "line 60\n" not in p.stderr
    assert len(p.stderr) <= 6000


def test_stop_missing_tool_blocks_naming_it(repo):
    setup(repo, profile(gate="no_such_tool_powerpuff_xyz"))
    dirty(repo)
    p = stop(repo)
    assert p.returncode == 2 and "`chk`" in p.stderr and "tool missing" in p.stderr


def test_stop_driver_env_is_noop(repo):
    marker = setup(repo, profile(), RED)
    dirty(repo)
    p = stop(repo, POWERPUFF_DRIVER="1")
    assert p.returncode == 0 and runs(marker) == 0


def test_stop_loop_guard_and_reset(repo):
    setup(repo, profile(hooks="  stop_gates: true\n  stop_max_blocks: 2\n"), RED)
    dirty(repo)
    assert [stop(repo).returncode for _ in range(2)] == [2, 2]
    p = stop(repo)
    assert p.returncode == 0 and "gates still red after 2 attempts" in p.stderr and "stopping for the human" in p.stderr
    assert stop(repo).returncode == 2
    (repo / "gate.py").write_text(GREEN.format(marker=repo.parent / "marker"), encoding="utf-8")
    assert stop(repo).returncode == 0
    state = json.loads((repo / ".powerpuff" / "hook-state.json").read_text(encoding="utf-8"))
    assert "s1" not in state["blocks"]


def test_stop_cache_hit_does_not_rerun(repo):
    marker = setup(repo, profile())
    dirty(repo)
    assert stop(repo).returncode == 0 and runs(marker) == 1
    assert stop(repo).returncode == 0 and runs(marker) == 1
    dirty(repo, "changed")
    os.utime(repo / "new.txt", (1, 1))
    assert stop(repo).returncode == 0 and runs(marker) == 2


def test_stop_internal_error_allows(repo):
    setup(repo, "hooks:\n  stop_gates: true\n")
    dirty(repo)
    p = stop(repo)
    assert p.returncode == 0 and p.stderr.startswith("powerpuff hook error:")
