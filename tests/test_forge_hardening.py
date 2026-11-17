"""Hardening of forge_run: additive test-writing sessions, green-base check, structured questions (fake claude only)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))
import forge_run  # noqa: E402
from fake_claude import GATE  # noqa: E402
from test_forge_run import BAD, FINAL, Harness  # noqa: E402

PROD = {"app.py": "def foo():\n    return 99\n\n\ndef bar():\n    return 99\n"}
MARK = ('python -c "import os; open(\'.forge/runs.txt\', \'a\').write(\'T\' if os.path.exists(\'tests\') else \'B\')"')
GREEN = "python -c pass"
BLOCKING = ("R1: foo returns 42\nR2: bar returns 7\nD1: rounding -> floor - existing code\n"
            "Q1 [blocking]: Delete old rows? | default: D1 | cost of a wrong guess: data loss\n")


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    x = Harness(tmp_path)
    x.base_check = True
    return x


def put_profile(h: Harness, commands: dict, extra: str = "") -> None:
    cmds = "".join(f"  {k}: '{v}'\n" for k, v in commands.items())
    (h.repo / "powerpuff.yaml").write_text(
        f"approved: true\ngates:\n  iteration: [{', '.join(commands)}]\ncommands:\n{cmds}{extra}", encoding="utf-8")


def audit(gates: dict, requirements: str | None = None) -> dict:
    write = {".forge/gates.json": json.dumps({"format_fix": None, "gates": gates})}
    if requirements:
        write[".forge/requirements.md"] = requirements
    return {"audit": [{"base": True, "write": write}]}


def stage_tests(**write: str) -> dict:
    return {"tests": [{"base": True, "write": write}]}


def git(h: Harness, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=h.repo, capture_output=True, text=True, check=True).stdout.strip()


def questions(h: Harness) -> list[dict]:
    return json.loads((h.repo / ".forge" / "questions.json").read_text(encoding="utf-8"))


# ---- change 1: test-writing sessions only add files ------------------------------------------------
def test_tests_stage_production_edit_is_restored(h: Harness) -> None:
    h.scenario(stage_tests(**PROD))
    r = h.run("Build foo", "--allow-red-base")
    assert r.rc == 0, r.err
    st = h.state()
    assert st["tests_stage_reverted"] == ["app.py"]
    assert "- test-writer changes reverted: 1 file(s)" in h.report()
    assert "return 99" not in git(h, "show", f"{st['frozen_sha']}:app.py")
    assert "tests: reverted app.py" in (h.repo / ".forge" / "progress.md").read_text(encoding="utf-8")


def test_tests_stage_stray_non_test_file_is_deleted(h: Harness) -> None:
    h.scenario(stage_tests(**{"helper_prod.py": "X = 1\n"}))
    assert h.run("Build foo", "--allow-red-base").rc == 0
    assert not (h.repo / "helper_prod.py").exists()
    assert h.state()["tests_stage_reverted"] == ["helper_prod.py"]


def test_tests_stage_manifest_edit_is_kept(h: Harness) -> None:
    (h.repo / "package.json").write_text('{"name": "x"}\n', encoding="utf-8")
    git(h, "add", "-A")
    git(h, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--no-gpg-sign", "-m", "manifest")
    h.scenario(stage_tests(**{"package.json": '{"name": "x", "devDependencies": {"vitest": "1"}}\n'}))
    assert h.run("Build foo", "--allow-red-base").rc == 0
    assert "vitest" in (h.repo / "package.json").read_text(encoding="utf-8")
    assert "tests_stage_reverted" not in h.state()
    assert "test-writer changes reverted" not in h.report()


def test_profile_test_infra_extends_the_allow_list(h: Harness) -> None:
    put_profile(h, {"unit": GATE}, "test_infra: [app.py]\n")
    h.scenario(stage_tests(**PROD))
    assert h.run("Build foo", "--allow-red-base").rc == 0
    assert "tests_stage_reverted" not in h.state()


def test_support_file_next_to_acceptance_file_is_kept(h: Harness) -> None:
    h.scenario(stage_tests(**{"tests/sample.json": "{}\n"}))
    assert h.run("Build foo", "--allow-red-base").rc == 0
    assert (h.repo / "tests" / "sample.json").is_file()
    assert "tests_stage_reverted" not in h.state()


# ---- change 2: green base before the run spends anything -------------------------------------------
def test_red_base_with_profile_fails_before_any_session(h: Harness) -> None:
    put_profile(h, {"unit": GATE})
    r = h.run("Build foo")
    assert r.rc == 1 and FINAL(r.out) == "FORGE: failed"
    assert h.calls() == []
    rep = h.report()
    assert "base is red: unit" in rep and "## Base check" in rep and "AssertionError" in rep
    assert h.state()["stage"] == "audit"


def test_allow_red_base_continues_and_gate_stays_required(h: Harness) -> None:
    put_profile(h, {"unit": GATE})
    r = h.run("Build foo", "--allow-red-base")
    assert r.rc == 0, r.err
    assert h.state()["base_red"] == ["unit"]
    assert "WARNING: base is red (unit)" in r.out
    assert "- base was red (allowed by --allow-red-base): unit" in h.report()


def test_allow_red_base_gate_is_still_required_at_the_end(h: Harness) -> None:
    put_profile(h, {"unit": GATE})
    h.scenario({"implement": [{"base": True, "write": BAD}], "fix": [{"base": True, "write": BAD}]})
    r = h.run("Build foo", "--allow-red-base")
    assert r.rc == 1 and FINAL(r.out) == "FORGE: failed"
    assert "unit=FAIL" in h.report()


def test_missing_tool_on_base_fails_even_with_the_flag(h: Harness) -> None:
    put_profile(h, {"unit": "definitely_not_a_tool_xyz --version"})
    r = h.run("Build foo", "--allow-red-base")
    assert r.rc == 1 and FINAL(r.out) == "FORGE: failed"
    assert h.calls() == []
    assert "missing tool 'definitely_not_a_tool_xyz'" in h.report()


def test_model_drafted_gates_are_checked_after_audit_before_tests(h: Harness) -> None:
    h.scenario(audit({"unit": GATE}))
    r = h.run("Build foo")
    assert r.rc == 1 and FINAL(r.out) == "FORGE: failed"
    assert h.stages() == ["audit"]
    assert "base is red: unit" in h.report()
    assert h.state()["stage"] == "tests"
    h.scenario(audit({"unit": GATE}))
    r2 = h.run("Build foo", "--allow-red-base")
    assert r2.rc == 0, r2.err
    assert h.stages() == ["audit", "tests", "implement", "accept"]


def test_resume_does_not_repeat_the_base_check(h: Harness) -> None:
    h.scenario({**audit({"unit": MARK}), "tests": [{"kill_driver": True}, "default"]})
    h.run("Build foo")
    assert h.state()["base_checked"] is True
    assert (h.repo / ".forge" / "runs.txt").read_text(encoding="utf-8") == "B"
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    assert (h.repo / ".forge" / "runs.txt").read_text(encoding="utf-8").count("B") == 1


# ---- change 3: structured questions ----------------------------------------------------------------
def test_parse_question_well_formed_and_malformed() -> None:
    q = forge_run.parse_question("Q1 [blocking]: Delete old rows? | default: D2 | cost of a wrong guess: data loss")
    assert q == {"id": "Q1", "class": "blocking", "question": "Delete old rows?", "default": "D2",
                 "wrong_guess_cost": "data loss", "answer": None}
    q = forge_run.parse_question("- **Q4** [Non-Blocking]: a | b? | default: x | cost of a wrong guess: rework")
    assert q["id"] == "Q4" and q["class"] == "non-blocking" and "unparsed" not in q
    for line in ("Q2: anything else?", "Q3 [info]: no cost here | default: y", "Q5 [urgent]: x | default: y | cost of "
                 "a wrong guess: z"):
        q = forge_run.parse_question(line)
        assert q["class"] == "non-blocking" and q["unparsed"] is True and q["question"] == line
        assert q["answer"] is None and q["id"] == line.split()[0].rstrip(":")


def test_default_fake_audit_question_is_unparsed_and_non_blocking(h: Harness) -> None:
    r = h.run("Build foo", "--preflight")
    assert r.rc == 0
    assert questions(h) == [{"id": "Q1", "class": "non-blocking", "question": "Q1: anything else?", "default": "",
                             "wrong_guess_cost": "", "answer": None, "unparsed": True}]


def test_blocking_question_fails_without_guessing(h: Harness) -> None:
    h.scenario(audit({"unit": GATE}, BLOCKING))
    r = h.run("Build foo", "--allow-red-base")
    assert r.rc == 1 and FINAL(r.out) == "FORGE: failed"
    assert h.stages() == ["audit"]
    assert "blocking questions: Q1" in h.report()
    assert questions(h)[0]["class"] == "blocking"


def test_assume_defaults_proceeds_and_report_lists_them(h: Harness) -> None:
    h.scenario(audit({"unit": GATE}, BLOCKING))
    r = h.run("Build foo", "--allow-red-base", "--assume-defaults")
    assert r.rc == 0, r.err
    assert h.stages() == ["audit", "tests", "implement", "accept"]
    rep = h.report()
    assert "## Assumed defaults" in rep and "- Q1: Delete old rows? -> default: D1" in rep
    assert "### blocking" in rep and "cost of a wrong guess: data loss | answer: (none)" in rep


def test_answered_question_proceeds_and_reaches_writer_and_builder(h: Harness) -> None:
    h.scenario(audit({"unit": GATE}, BLOCKING))
    assert h.run("Build foo", "--allow-red-base").rc == 1
    qs = questions(h)
    qs[0]["answer"] = "use soft delete"
    (h.repo / ".forge" / "questions.json").write_text(json.dumps(qs), encoding="utf-8")
    r = h.run("Build foo", "--allow-red-base")
    assert r.rc == 0, r.err
    assert h.stages() == ["audit", "tests", "implement", "accept"]
    for call in h.calls()[1:3]:
        assert "Human decisions:" in call["prompt"] and "Delete old rows? => use soft delete" in call["prompt"]
    assert "answer: use soft delete" in h.report()
    assert "Human decisions:" not in h.calls()[0]["prompt"]


def test_preflight_prints_counts_and_path(h: Harness) -> None:
    reqs = (BLOCKING + "Q2 [non-blocking]: Format? | default: D1 | cost of a wrong guess: rework\n"
            "Q3 [info]: Assumed UTC | default: UTC | cost of a wrong guess: none\nQ4: free text\n")
    h.scenario(audit({"unit": GATE}, reqs))
    r = h.run("Build foo", "--preflight")
    assert r.rc == 0 and FINAL(r.out) == "FORGE: preflight"
    assert "blocking=1 non-blocking=2 info=1" in r.out
    assert "questions.json" in r.out
    assert h.stages() == ["audit"]
