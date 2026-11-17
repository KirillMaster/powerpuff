"""quality_report: schema 1, per-gate timing, exit codes, missing tool, tiers and placeholders."""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import quality_report  # noqa: E402

OK = 'python -c "pass"'
BAD = 'python -c "raise SystemExit(1)"'
SLOW = 'python -c "import time; time.sleep(0.3)"'
NOTOOL = "powerpuff-no-such-tool-xyz --version"


def repo(tmp_path: Path, gates: dict, commands: dict, approved: str = "true", git: bool = False) -> Path:
    lines = [f"approved: {approved}", "gates:"]
    lines += [f"  {t}: [{', '.join(ids)}]" for t, ids in gates.items()]
    lines.append("commands:")
    lines += [f"  {k}: '{v}'" for k, v in commands.items()]
    (tmp_path / "powerpuff.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if git:
        run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)  # noqa: E731
        run("init", "-q")
        run("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
        run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x")
    return tmp_path


def report(root: Path, name: str = "quality-report.json") -> dict:
    return json.loads((root / name).read_text(encoding="utf-8"))


def test_all_green_schema_and_timing(tmp_path, capsys):
    root = repo(tmp_path, {"iteration": ["a"], "unit": ["slow"]}, {"a": OK, "slow": SLOW}, git=True)
    assert quality_report.main(["--repo", str(root)]) == 0
    rep = report(root)
    assert rep["schema"] == 1 and rep["ok"] is True and rep["tier"] == "all" and rep["profile_approved"] is True
    assert len(rep["head"]) == 40 and rep["base"] == rep["head"] and rep["generated_at"].endswith("Z")
    assert [g["name"] for g in rep["gates"]] == ["a", "slow"] and [g["tier"] for g in rep["gates"]] == ["iteration", "unit"]
    slow = rep["gates"][1]
    assert slow["ok"] and slow["exit_code"] == 0 and slow["seconds"] >= 0.3 and slow["missing_tool"] is False
    assert set(slow) == {"name", "tier", "ok", "exit_code", "seconds", "missing_tool", "output_tail"}
    out = capsys.readouterr().out
    assert "gate" in out and "result" in out and "seconds" in out and "slow" in out and "pass" in out


def test_red_gate_exit_1_with_tail(tmp_path, capsys):
    cmd = 'python -c "import sys; print(4242); sys.exit(1)"'
    root = repo(tmp_path, {"iteration": ["good", "bad"]}, {"good": OK, "bad": cmd})
    assert quality_report.main(["--repo", str(root)]) == 1
    rep = report(root)
    assert rep["ok"] is False and rep["head"] is None
    assert [g["ok"] for g in rep["gates"]] == [True, False]
    assert "4242" in rep["gates"][1]["output_tail"] and rep["gates"][1]["exit_code"] == 1
    assert "FAIL" in capsys.readouterr().out


def test_missing_tool_exit_3_and_flag(tmp_path, capsys):
    root = repo(tmp_path, {"iteration": ["x"]}, {"x": NOTOOL})
    assert quality_report.main(["--repo", str(root)]) == 3
    gate = report(root)["gates"][0]
    assert gate["ok"] is False and gate["missing_tool"] is True
    assert "MISSING TOOL" in capsys.readouterr().out


def test_missing_tool_wins_over_red(tmp_path):
    root = repo(tmp_path, {"iteration": ["bad", "x"]}, {"bad": BAD, "x": NOTOOL})
    assert quality_report.main(["--repo", str(root)]) == 3


def test_no_profile_exit_3(tmp_path, capsys):
    assert quality_report.main(["--repo", str(tmp_path)]) == 3
    assert "no profile" in capsys.readouterr().err
    assert not (tmp_path / "quality-report.json").exists()


def test_invalid_profile_exit_3(tmp_path, capsys):
    (tmp_path / "powerpuff.yaml").write_text("approved: true\ngates:\n  unit: [ghost]\ncommands:\n  real: x\n",
                                             encoding="utf-8")
    assert quality_report.main(["--repo", str(tmp_path)]) == 3
    assert "ghost" in capsys.readouterr().err


def test_tier_selection(tmp_path):
    root = repo(tmp_path, {"iteration": ["i"], "unit": ["u"], "final": ["f"]}, {"i": OK, "u": OK, "f": BAD})
    assert quality_report.main(["--repo", str(root), "--tier", "iteration"]) == 0
    assert [g["name"] for g in report(root)["gates"]] == ["i"] and report(root)["tier"] == "iteration"
    assert quality_report.main(["--repo", str(root), "--tier", "unit"]) == 0
    assert [g["name"] for g in report(root)["gates"]] == ["u"]
    assert quality_report.main(["--repo", str(root), "--tier", "final"]) == 1
    assert quality_report.main(["--repo", str(root), "--tier", "all"]) == 1
    assert [g["name"] for g in report(root)["gates"]] == ["i", "u", "f"]


def test_empty_tier_exit_3(tmp_path, capsys):
    root = repo(tmp_path, {"iteration": ["i"]}, {"i": OK})
    assert quality_report.main(["--repo", str(root), "--tier", "final"]) == 3
    assert "no gates" in capsys.readouterr().err


def test_unapproved_profile_is_recorded(tmp_path, capsys):
    root = repo(tmp_path, {"iteration": ["i"]}, {"i": OK}, approved="false")
    assert quality_report.main(["--repo", str(root)]) == 0
    assert report(root)["profile_approved"] is False
    assert "NOT approved" in capsys.readouterr().err


def test_base_placeholder_and_out_path(tmp_path):
    root = repo(tmp_path, {"unit": ["b"]},
                {"b": 'python -c "import os, sys; sys.exit(0 if os.environ[sys.argv[2]] == sys.argv[1] else 1)" {base} POWERPUFF_BASE'},
                git=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    out = tmp_path / "sub" / "r.json"
    out.parent.mkdir()
    assert quality_report.main(["--repo", str(root), "--base", sha, "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["base"] == sha


def test_timeout_is_a_failure(tmp_path):
    root = repo(tmp_path, {"iteration": ["t"]}, {"t": 'python -c "import time; time.sleep(3)"'})
    assert quality_report.main(["--repo", str(root), "--timeout", "1"]) == 1
    gate = report(root)["gates"][0]
    assert gate["ok"] is False and "timeout" in gate["output_tail"]