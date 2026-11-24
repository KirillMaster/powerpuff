"""Gate command placeholders {base} / {root} and env POWERPUFF_BASE / POWERPUFF_ROOT, tier `final` in the queue, --final."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts"))
import forge_check  # noqa: E402
from test_forge_run import FINAL, Harness  # noqa: E402
from test_hooks import dirty, git, profile as hook_profile, repo, setup, stop  # noqa: E402,F401
from test_queue_run import QH, unit  # noqa: E402

DIFF_GATES = ROOT / "scripts" / "diff_gates.py"


@pytest.fixture
def h(tmp_path: Path) -> QH:
    return QH(tmp_path)


def posix(p: Path) -> str:
    return str(p).replace(chr(92), "/")


def py(code: str) -> str:
    return f'python -c "{code}"'


def log_cmd(log: Path) -> str:
    return py("import os;open(r'" + posix(log) + "','a').write('{base}|'+os.environ['POWERPUFF_BASE']+'|{root}|'"
              "+os.environ['POWERPUFF_ROOT']+chr(10))")


def lines(log: Path) -> list[list[str]]:
    return [x.split("|") for x in log.read_text(encoding="utf-8").splitlines()]


def test_expand_and_unknown_braces_untouched(tmp_path: Path) -> None:
    ctx = forge_check.gate_context(tmp_path, "abc123")
    assert forge_check.expand("x --base {base} --root {root} {keep} {{base}}", ctx) == \
        f"x --base abc123 --root {posix(tmp_path)} {{keep}} {{abc123}}"
    assert forge_check.expand("{base}", None) == "{base}"
    assert forge_check.expand("{base}", forge_check.gate_context(tmp_path, "")) == "{base}"


def test_check_gates_substitutes_and_exports_env(tmp_path: Path) -> None:
    out = tmp_path / "o.txt"
    cmd = py("import os;open(r'" + posix(out) + "','w').write('{base} {keep} '+os.environ['POWERPUFF_BASE']+' '"
             "+os.environ['POWERPUFF_ROOT'])")
    res = forge_check.check_gates(tmp_path, 30, {"g": cmd}, forge_check.gate_context(tmp_path, "deadbeef"))
    assert res["ok"] is True
    assert out.read_text(encoding="utf-8") == f"deadbeef {{keep}} deadbeef {posix(tmp_path)}"


def test_tail_chars_parameter(tmp_path: Path) -> None:
    cmd = py("print('x'*3000);raise SystemExit(1)")
    assert len(forge_check.check_gates(tmp_path, 30, {"g": cmd})["results"]["g"]["tail"]) == 600
    assert len(forge_check.check_gates(tmp_path, 30, {"g": cmd}, tail_chars=2000)["results"]["g"]["tail"]) == 2000


@pytest.mark.parametrize("gate", ["size", "duplication"])
def test_diff_gates_base_equals_head_passes(tmp_path: Path, gate: str) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    p = subprocess.run([sys.executable, str(DIFF_GATES), gate, "--base", "HEAD", "--root", str(tmp_path)],
                       capture_output=True, text=True, encoding="utf-8")
    assert p.returncode == 0, p.stdout + p.stderr


# ---- forge_run -----------------------------------------------------------------------------------------------------

def put_json_profile(h: Harness, gates: dict, commands: dict) -> None:
    (h.repo / "powerpuff.gates.json").write_text(json.dumps({"approved": True, "gates": gates, "commands": commands}),
                                                  encoding="utf-8")


def test_forge_base_and_unit_checks_get_context(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.base_check = True
    log = tmp_path / "ctx.log"
    put_json_profile(h, {"iteration": ["ctx"]}, {"ctx": log_cmd(log)})
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    rows = lines(log)
    start = h.state()["start_sha"]
    assert len(rows) >= 2 and all(b == e == start and rt == ro == posix(h.repo) for b, e, rt, ro in rows)
    assert rows[0][0] == start


def test_forge_prompt_has_substituted_commands(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    put_json_profile(h, {"iteration": ["ctx"]}, {"ctx": "python -c \"print(1)\" --base {base} {keep}"})
    assert h.run("Build foo").rc == 0
    prompt = next(c for c in h.calls() if c["stage"] == "implement")["prompt"]
    assert f"--base {h.state()['start_sha']} {{keep}}" in prompt


def final_profile(h: Harness, flag: Path) -> None:
    put_json_profile(h, {"iteration": ["ok"], "final": ["fin"]},
                     {"ok": "python -c \"pass\"",
                      "fin": py("import os,sys;sys.exit(0 if os.path.exists(r'" + posix(flag) + "') else 1)")})


def test_forge_final_not_run_without_flag(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    final_profile(h, tmp_path / "flag")
    r = h.run("Build foo")
    assert r.rc == 0 and FINAL(r.out) == "FORGE: done"
    assert "final_results" not in h.state()


def test_forge_final_pass(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    flag = tmp_path / "flag"
    flag.write_text("1", encoding="utf-8")
    final_profile(h, flag)
    r = h.run("Build foo", "--final")
    assert r.rc == 0 and FINAL(r.out) == "FORGE: done", r.err
    assert h.state()["final_results"] == {"fin": True}
    assert "- final gates: {'fin': True}" in h.report()


def test_forge_final_fail_then_resume(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    flag = tmp_path / "flag"
    final_profile(h, flag)
    r = h.run("Build foo", "--final")
    assert r.rc == 1 and FINAL(r.out) == "FORGE: failed"
    assert "final gates red: fin" in h.report().splitlines()[0]
    assert h.state()["stage"] == "report"
    calls = len(h.calls())
    flag.write_text("1", encoding="utf-8")
    r = h.run("Build foo", "--final")
    assert r.rc == 0 and FINAL(r.out) == "FORGE: done", r.err
    assert len(h.calls()) == calls


# ---- queue_run -----------------------------------------------------------------------------------------------------

def queue_profile(h: QH, final: list[str], commands: dict, approved: bool = True) -> None:
    (h.repo / "powerpuff.gates.json").write_text(json.dumps(
        {"approved": approved, "gates": {"iteration": ["unit"], "final": final},
         "commands": {"unit": "python -c \"pass\"", **commands}}), encoding="utf-8")


def test_queue_final_commands_get_context(h: QH) -> None:
    log = h.tmp / "ctx.log"
    r = h.run(h.queue([unit("U1")], final={"commands": {"ctx": log_cmd(log)}}))
    assert r.rc == 0, r.out
    (base, env_base, root, env_root), = lines(log)
    assert base == env_base == h.state()["base_sha"]
    assert root == env_root and root.endswith("/wt/_integration")
    assert h.summary()["final_gates_source"] == "queue"


def test_queue_runs_profile_final_tier_with_queue_override(h: QH) -> None:
    ok = "python -c \"open('p_ok.txt','w')\""
    queue_profile(h, ["fin_a", "fin_b"], {"fin_a": ok, "fin_b": "python -c \"raise SystemExit(1)\""})
    q = h.queue([unit("U1")], final={"commands": {"fin_b": "python -c \"pass\""}})
    r = h.run(q)
    assert r.rc == 0 and r.last == "QUEUE: done", r.out
    s = h.summary()
    assert set(s["final"]) == {"fin_a", "fin_b"} and all(v["ok"] for v in s["final"].values())
    assert s["final_gates_source"] == "approved profile + queue"
    assert "gates source: approved profile + queue" in (h.run_dir / "SUMMARY.md").read_text(encoding="utf-8")


def test_queue_profile_final_failure_is_partial_and_unapproved_flagged(h: QH) -> None:
    queue_profile(h, ["fin"], {"fin": "python -c \"raise SystemExit(4)\""}, approved=False)
    r = h.run(h.queue([unit("U1")]))
    assert r.rc == 2 and r.last == "QUEUE: partial", r.out
    s = h.summary()
    assert s["final"]["fin"]["ok"] is False and s["final"]["fin"]["code"] == 4
    assert s["final_gates_source"] == "profile NOT APPROVED"
    assert "gates source: profile NOT APPROVED" in (h.run_dir / "SUMMARY.md").read_text(encoding="utf-8")


def test_queue_final_missing_tool_fails(h: QH) -> None:
    queue_profile(h, ["fin"], {"fin": "definitely_not_a_tool_xyz --version"})
    r = h.run(h.queue([unit("U1")]))
    assert r.last == "QUEUE: partial", r.out
    assert h.summary()["final"]["fin"]["missing_tool"] is True


def test_queue_without_profile_and_final_has_no_contour(h: QH) -> None:
    r = h.run(h.queue([unit("U1")]))
    assert r.last == "QUEUE: done"
    assert h.summary()["final"] is None and h.summary()["final_gates_source"] is None


# ---- hook_stop -----------------------------------------------------------------------------------------------------

CTX_GATE = ("import os, sys\nopen(r'{marker}', 'w').write(' '.join(sys.argv[1:]) + '|' + os.environ['POWERPUFF_BASE']"
            " + '|' + os.environ['POWERPUFF_ROOT'])\nsys.exit(0)\n")


def test_hook_stop_substitutes_head_and_root(repo):
    marker = setup(repo, hook_profile(gate="python gate.py {base} {keep}"), CTX_GATE)
    dirty(repo)
    p = stop(repo)
    assert p.returncode == 0, p.stderr
    args, env_base, env_root = marker.read_text(encoding="utf-8").split("|")
    assert args == "HEAD {keep}" and env_base == "HEAD" and env_root == posix(repo.resolve())
