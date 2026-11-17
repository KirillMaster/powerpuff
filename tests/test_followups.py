"""{plugin} placeholder, exit_code / seconds of gate results, .forge/quality-report.json of forge_run, portable profiles."""

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts"))
import forge_check  # noqa: E402
import profile_init  # noqa: E402
from test_forge_run import FINAL, Harness  # noqa: E402
from test_gate_context import final_profile, posix, py, put_json_profile  # noqa: E402
from test_profile_init import STACKS, init, profile_of, put, tools  # noqa: E402
from test_queue_run import QH, unit  # noqa: E402

KEYS = {"name", "tier", "ok", "exit_code", "seconds", "missing_tool", "output_tail"}


def read_report(h: Harness) -> dict:
    return json.loads((h.repo / ".forge" / "quality-report.json").read_text(encoding="utf-8"))


def assert_schema(rep: dict) -> None:
    assert rep["schema"] == 1 and rep["tool"] == "powerpuff.forge_run" and rep["generated_at"].endswith("Z")
    assert isinstance(rep["profile_approved"], bool) and rep["base"] and rep["head"] and isinstance(rep["ok"], bool)
    assert rep["gates"] and all(set(g) == KEYS for g in rep["gates"])
    assert rep["ok"] == all(g["ok"] for g in rep["gates"])


def test_plugin_placeholder_expands_and_is_exported(tmp_path: Path) -> None:
    ctx = forge_check.gate_context(tmp_path, "abc")
    plugin = posix(ROOT)
    assert ctx["plugin"] == plugin and (Path(plugin) / "scripts" / "forge_check.py").is_file()
    assert forge_check.expand('python "{plugin}/scripts/x.py" {base}', ctx) == f'python "{plugin}/scripts/x.py" abc'
    out = tmp_path / "o.txt"
    cmd = py("import os;open(r'" + posix(out) + "','w').write('{plugin}|'+os.environ['POWERPUFF_PLUGIN'])")
    assert forge_check.check_gates(tmp_path, 30, {"g": cmd}, ctx)["ok"] is True
    assert out.read_text(encoding="utf-8") == f"{plugin}|{plugin}"


def test_gate_results_carry_exit_code_and_seconds(tmp_path: Path) -> None:
    gates = {"ok": py("pass"), "red": py("raise SystemExit(7)"), "slow": py("import time;time.sleep(5)"),
             "gone": "definitely-not-a-tool-xyz"}
    res = forge_check.check_gates(tmp_path, 30, {k: v for k, v in gates.items() if k != "slow"})["results"]
    assert res["ok"]["exit_code"] == 0 and res["red"]["exit_code"] == 7 and res["red"]["ok"] is False
    assert res["gone"]["exit_code"] != 0 and res["gone"].get("missing_tool") is True
    assert all(isinstance(r["seconds"], float) and r["seconds"] >= 0 for r in res.values())
    slow = forge_check.check_gates(tmp_path, 1, {"slow": gates["slow"]})["results"]["slow"]
    assert slow["ok"] is False and slow["exit_code"] is None and slow["seconds"] >= 1.0 and "timeout" in slow["tail"]


def test_forge_done_writes_quality_report(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    put_json_profile(h, {"iteration": ["ok"]}, {"ok": py("pass")})
    r = h.run("Build foo")
    assert r.rc == 0 and FINAL(r.out) == "FORGE: done", r.err
    rep = read_report(h)
    assert_schema(rep)
    assert rep["ok"] is True and rep["profile_approved"] is True and rep["base"] == h.state()["start_sha"]
    assert rep["guard"]["ok"] is True
    assert [(g["name"], g["tier"], g["exit_code"]) for g in rep["gates"]] == [("ok", "iteration", 0)]
    text = h.report()
    assert "- quality report: .forge/quality-report.json" in text and "| ok | pass |" in text


def test_forge_failed_writes_quality_report(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    final_profile(h, tmp_path / "missing-flag")
    r = h.run("Build foo", "--final")
    assert r.rc == 1 and FINAL(r.out) == "FORGE: failed"
    rep = read_report(h)
    assert_schema(rep)
    by = {g["name"]: g for g in rep["gates"]}
    assert rep["ok"] is False and by["ok"]["ok"] is True and by["fin"]["ok"] is False
    assert by["fin"]["tier"] == "final" and by["fin"]["exit_code"] == 1
    assert "| fin | FAIL |" in h.report()


def test_queue_snapshot_keeps_quality_report(tmp_path: Path) -> None:
    h = QH(tmp_path)
    r = h.run(h.queue([unit("U1")]))
    assert r.rc == 0, r.out
    rep = json.loads((h.run_dir / "forge" / "U1" / "quality-report.json").read_text(encoding="utf-8"))
    assert rep["schema"] == 1 and rep["gates"]


def test_profile_init_emits_portable_plugin_placeholder(tmp_path: Path, monkeypatch, capsys) -> None:
    put(tmp_path, "go.mod", "module x")
    put(tmp_path, "a_test.go", "package x")
    tools(monkeypatch, "go", "gofmt", "python")
    _, out, _ = init(tmp_path, capsys)
    cmd = profile_of(out, tmp_path)["gates"]["duplication"]
    assert '"{plugin}/scripts/diff_gates.py"' in cmd
    assert posix(ROOT) not in out and str(ROOT) not in out


@pytest.mark.parametrize("stack", STACKS)
def test_preset_loads_and_stays_portable(tmp_path: Path, monkeypatch, capsys, stack: str) -> None:
    tools(monkeypatch, everything=True)
    _, out, _ = init(tmp_path, capsys, "--stack", stack)
    prof = profile_of(out, tmp_path)
    assert prof["gates"] and all("{plugin}" in c or "diff_gates" not in c for c in prof["gates"].values())
    assert posix(ROOT) not in out and str(profile_init.PLUGIN) not in out
