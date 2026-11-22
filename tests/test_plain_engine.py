"""The plain engine of forge_run (one model context, `--resume`) against tests/fake_claude.py; queue_run passes `engine`."""

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
import queue_run  # noqa: E402
from fake_claude import EXTRA_TEST, GATE, TEST_FILE  # noqa: E402
from test_forge_profile import profile  # noqa: E402
from test_forge_run import BAD, FINAL, Harness  # noqa: E402
from test_queue_run import QH, unit  # noqa: E402

API = {"result": "api_error"}
PAUSE = "FORGE: paused (API unreachable)"
BLOCKING = "Q1 [blocking]: which db? | default: sqlite | cost of a wrong guess: rewrite\n"
PROFILE = profile(extra="test_gates: [unit]\n")


class PH(Harness):
    def __init__(self, tmp: Path, prof: str | None = PROFILE):
        super().__init__(tmp)
        if prof:
            (self.repo / "powerpuff.yaml").write_text(prof, encoding="utf-8")

    def run(self, spec: str = "Build foo", *args: str, **kw):  # type: ignore[override]
        return super().run(spec, "--engine", "plain", *args, **kw)

    def of(self, stage: str) -> list[dict]:
        return [c for c in self.calls() if c["stage"] == stage]


@pytest.fixture
def h(tmp_path: Path) -> PH:
    return PH(tmp_path)


def test_happy_path_tests_then_a_fresh_build_session(h: PH) -> None:
    r = h.run()
    assert r.rc == 0, r.err
    assert FINAL(r.out) == "FORGE: done"
    assert h.stages() == ["ptests", "pbuild"]
    t1, t2 = h.calls()
    assert not t1["has_resume"] and not t2["has_resume"] and "Build foo" in t2["prompt"]
    assert {c["model"] for c in h.calls()} == {"strong"}
    assert t1["prompt"].endswith("Build foo") and "[blocking|non-blocking|info]" in t1["prompt"]
    assert "tests/test_acceptance.py" in t2["prompt"] and GATE in t2["prompt"]
    st = h.state()
    assert (st["engine"], st["stage"], st["status"]) == ("plain", "finished", "done")
    assert st["red_on_base"] is True and st["cost"]["sessions"] == 2
    assert "tests/test_acceptance.py" in (h.repo / ".forge" / "frozen.sha256").read_text(encoding="utf-8")
    assert json.loads((h.repo / ".forge" / "questions.json").read_text(encoding="utf-8"))[0]["class"] == "non-blocking"
    assert not (h.repo / ".forge" / "frozen.json").exists()
    assert "engine: plain, fix rounds used: 0" in h.report() and "unit=pass" in h.report()
    assert (h.repo / ".forge" / "quality-report.json").is_file()


def test_model_defaults_to_profile_then_opus(tmp_path: Path) -> None:
    for sub, extra, want in (("a", "model: sonnet\n", "sonnet"), ("b", "", "opus")):
        (tmp_path / sub).mkdir()
        h = PH(tmp_path / sub, profile(extra=extra))
        env = {**os.environ, "PATH": str(h.bindir) + os.pathsep + os.environ["PATH"], "FAKE_CLAUDE_SCENARIO": str(h.scn)}
        cmd = [sys.executable, str(ROOT / "scripts" / "forge_run.py"), "Build foo", "--repo", str(h.repo), "--skip-base-check"]
        assert subprocess.run(cmd, cwd=h.tmp, env=env, capture_output=True).returncode == 0
        assert {c["model"] for c in h.calls()} == {want}


def test_no_profile_fails_before_any_session(tmp_path: Path) -> None:
    h = PH(tmp_path, None)
    r = h.run()
    assert r.rc == 1 and FINAL(r.out) == "FORGE: failed" and h.calls() == []
    assert "needs a gate profile" in h.report()


def test_no_profile_drafts_gates_from_stack_presets(tmp_path: Path) -> None:
    h = PH(tmp_path, None)
    h.base_check = True
    (h.repo / "pyproject.toml").write_text("[project]\nname = 'foo'\nversion = '0.1.0'\n", encoding="utf-8")
    h.run()
    draft = h.repo / ".forge" / "powerpuff.draft.yaml"
    assert draft.is_file() and "approved: false" in draft.read_text(encoding="utf-8")
    assert "needs a gate profile" not in h.report()
    assert "base is red" not in h.report()  # unapproved draft: gates red on the untouched base are excluded, not fatal


def test_approved_profile_keeps_red_base_fatal(tmp_path: Path) -> None:
    h = PH(tmp_path, "approved: true\ngates:\n  iteration: [ok, bad]\ncommands:\n  ok: 'python -c \"pass\"'\n"
                     "  bad: 'python -c \"import sys; sys.exit(1)\"'\n")
    h.base_check = True
    r = h.run()
    assert FINAL(r.out) == "FORGE: failed" and "base is red: bad" in h.report()


def test_draft_profile_gate_with_missing_tool_is_excluded(tmp_path: Path) -> None:
    h = PH(tmp_path, "approved: false\ngates:\n  iteration: [ok, gone]\ncommands:\n  ok: 'python -c \"pass\"'\n"
                     "  gone: 'no-such-tool-pp --check'\n")
    h.base_check = True
    r = h.run()
    assert r.rc == 0, r.err
    assert "excluded: gone" in h.report()


def test_setup_runs_once_before_the_base_check(tmp_path: Path) -> None:
    h = PH(tmp_path, "approved: true\ngates:\n  iteration: [dep]\ncommands:\n"
                     "  dep: python -c \"import os, sys; sys.exit(0 if os.path.isfile('dep.txt') else 1)\"\n"
                     "setup: python -c \"open('dep.txt', 'w').write('x')\"\n")
    h.base_check = True
    r = h.run()
    assert r.rc == 0, h.report()
    assert h.state()["setup_done"] is True and "setup: ok" in (h.repo / ".forge" / "progress.md").read_text(encoding="utf-8")


def test_setup_failure_is_fatal_for_approved_and_a_warning_for_draft(tmp_path: Path) -> None:
    prof = "gates:\n  iteration: [ok]\ncommands:\n  ok: 'python -c \"pass\"'\nsetup: 'python -c \"import sys; sys.exit(3)\"'\n"
    (tmp_path / "a").mkdir()
    (tmp_path / "d").mkdir()
    h = PH(tmp_path / "a", "approved: true\n" + prof)
    h.base_check = True
    r = h.run()
    assert FINAL(r.out) == "FORGE: failed" and "setup failed" in h.report()
    d = PH(tmp_path / "d", "approved: false\n" + prof)
    d.base_check = True
    r = d.run()
    assert r.rc == 0, r.err
    assert "setup failed:" in d.report()


def test_approved_profile_missing_tool_stays_fatal(tmp_path: Path) -> None:
    h = PH(tmp_path, "approved: true\ngates:\n  iteration: [ok, gone]\ncommands:\n  ok: 'python -c \"pass\"'\n"
                     "  gone: 'no-such-tool-pp --check'\n")
    h.base_check = True
    r = h.run()
    assert FINAL(r.out) == "FORGE: failed" and "missing tool 'no-such-tool-pp'" in h.report()


def test_blocking_question_pauses_then_answers_resume_the_session(h: PH) -> None:
    h.scenario({"ptests": [{"base": True, "append": {".forge/requirements.md": BLOCKING.replace("Q1", "Q2")}}]})
    r = h.run()
    assert r.rc == 1 and FINAL(r.out) == "FORGE: failed"
    assert "blocking questions: Q2" in h.report() and h.state()["stage"] == "plain-tests"
    assert h.stages() == ["ptests"]
    qp = h.repo / ".forge" / "questions.json"
    qs = json.loads(qp.read_text(encoding="utf-8"))
    next(q for q in qs if q["id"] == "Q2")["answer"] = "postgres"
    qp.write_text(json.dumps(qs), encoding="utf-8")
    r2 = h.run()
    assert r2.rc == 0, r2.err
    assert h.stages() == ["ptests", "panswers", "pbuild"]
    ans, build = h.of("panswers")[0], h.of("pbuild")[0]
    assert ans["resume"] == h.of("ptests")[0]["session_id"] and "postgres" in ans["prompt"]
    assert not build["has_resume"] and "postgres" in build["prompt"]


def test_assume_defaults_goes_on(h: PH) -> None:
    h.scenario({"ptests": [{"base": True, "write": {".forge/requirements.md": BLOCKING}}]})
    r = h.run("Build foo", "--assume-defaults")
    assert r.rc == 0, r.err
    assert h.stages() == ["ptests", "pbuild"] and h.state()["assumed_defaults"] == ["Q1"]
    assert "Assumed defaults" in h.report()


def test_test_writer_edit_of_existing_file_is_reverted(h: PH) -> None:
    h.scenario({"ptests": [{"base": True, "write": {"app.py": "def foo():\n    return 1\n"}}]})
    r = h.run()
    assert r.rc == 0, r.err
    st = h.state()
    assert st["tests_stage_reverted"] == ["app.py"] and st["red_on_base"] is True
    assert "test-writer changes reverted: 1" in h.report()


def test_no_tests_one_resume_then_fail(h: PH) -> None:
    h.scenario({"ptests": [{}]})
    r = h.run()
    assert r.rc == 1 and h.stages() == ["ptests", "retry"]
    assert h.of("retry")[0]["resume"] == h.of("ptests")[0]["session_id"]
    assert "no acceptance tests written" in h.report()


def test_no_tests_second_chance_succeeds(h: PH) -> None:
    h.scenario({"ptests": [{}], "retry": [{"write": {"tests/test_acceptance.py": TEST_FILE}}]})
    r = h.run()
    assert r.rc == 0, r.err
    assert h.stages() == ["ptests", "retry", "pbuild"]


def test_vacuous_tests_resume_the_same_session(tmp_path: Path) -> None:
    h = PH(tmp_path, profile(commands={"unit": 'python -c "pass"'}, extra="test_gates: [unit]\n"))
    r = h.run()
    assert r.rc == 0, r.err
    assert h.stages() == ["ptests", "vacuous", "pbuild"]
    assert h.of("vacuous")[0]["resume"] == h.of("ptests")[0]["session_id"]
    assert not h.of("pbuild")[0]["has_resume"]
    assert h.state()["red_on_base"] is False
    assert "tests/test_acceptance_vacuous.py" in (h.repo / ".forge" / "frozen.sha256").read_text(encoding="utf-8")


def test_frozen_test_edited_in_build_is_restored_and_counted(h: PH) -> None:
    h.scenario({"pbuild": [{"base": True, "append": {"tests/test_acceptance.py": "\n# tampered\n"}}]})
    r = h.run()
    assert r.rc == 0, r.err
    assert h.stages() == ["ptests", "pbuild", "fix"]
    assert (h.repo / "tests" / "test_acceptance.py").read_text(encoding="utf-8") == TEST_FILE
    assert h.state()["frozen_violations"] == 1
    assert "changed_or_missing" in h.of("fix")[0]["prompt"]
    assert "frozen test violations (restored from git): 1" in h.report()


def test_red_gates_resume_fix_rounds_until_green(h: PH) -> None:
    h.scenario({"pbuild": [{"base": True, "write": BAD}], "fix": [{"base": True, "write": BAD, "append": {"notes.txt": "x"}}, "default"]})
    r = h.run()
    assert r.rc == 0, r.err
    assert h.stages() == ["ptests", "pbuild", "fix", "fix"]
    build, f1, f2 = h.of("pbuild")[0], *h.of("fix")
    assert f1["resume"] == build["session_id"] and f2["resume"] == f1["session_id"]
    brief = json.loads(f1["prompt"].split("forge_check output:\n", 1)[1])
    assert brief["gates"]["results"]["unit"]["ok"] is False
    assert h.state()["plain_round"] == 2 and "fix rounds used: 2" in h.report()


def test_fix_rounds_exhausted_is_failed(h: PH) -> None:
    h.scenario({"pbuild": [{"base": True, "write": BAD}], "fix": [{"base": True, "write": BAD, "append": {"notes.txt": "x"}}]})
    r = h.run("Build foo", "--max-fix-rounds", "2")
    assert r.rc == 1 and FINAL(r.out) == "FORGE: failed"
    assert h.stages() == ["ptests", "pbuild", "fix", "fix"]
    assert "gates red after 2 fix round(s)" in h.report()


def test_fix_round_without_change_stops_early(h: PH) -> None:
    h.scenario({"pbuild": [{"base": True, "write": BAD}], "fix": [{"base": True, "write": BAD}]})
    r = h.run()
    assert r.rc == 1 and FINAL(r.out) == "FORGE: failed"
    assert h.stages().count("fix") == 1 and "stuck: fix round 1 changed nothing" in h.report()


def test_resumed_session_cost_counts_only_the_increment(h: PH) -> None:
    h.scenario({"pbuild": [{"base": True, "write": BAD}], "fix": [{"base": True, "write": BAD, "append": {"notes.txt": "x"}}, "default"]})
    h.run()
    assert h.stages() == ["ptests", "pbuild", "fix", "fix"]
    assert h.state()["cost"]["total_usd"] == 1.0


def test_uncovered_spec_ids_get_one_cover_turn(h: PH) -> None:
    h.scenario({"pcover": [{"write": {"tests/test_r3.py": EXTRA_TEST}}]})
    r = h.run("R1: foo returns 42\nR2: bar returns 7\nR3: baz exists")
    assert r.rc == 0, r.err
    assert h.stages() == ["ptests", "pcover", "pbuild"]
    assert "R3" in h.of("pcover")[0]["prompt"] and "requirements: 3, uncovered: []" in h.report()


def test_yamlkit_ids_are_requirements(tmp_path: Path, h: PH) -> None:
    spec = tmp_path / "spec-dir"
    spec.mkdir()
    (spec / "spec.yaml").write_text(
        'user_stories:\n  - id: "US1"\n    acceptance_scenarios:\n      - id: "AS-1"\n        then: "foo is 42"\n'
        '      - id: "AS-2"\n        then: "bar is 7"\nfunctional_requirements:\n  - id: "FR-010"\n    text: "baz"\n',
        encoding="utf-8")
    h.scenario({"pcover": [{"write": {"tests/test_more.py": "def test_more():\n    # AS-1 AS-2 FR-010\n    pass\n"}}]})
    r = h.run(str(spec))
    assert r.rc == 0, r.err
    assert h.stages() == ["ptests", "pcover", "pbuild"]
    prompt = h.of("pcover")[0]["prompt"]
    assert "AS-1" in prompt and "FR-010" in prompt and "US1" not in prompt
    assert "requirements: 3, uncovered: []" in h.report()


def test_uncovered_spec_id_left_is_reported_not_fatal(h: PH) -> None:
    r = h.run("R1: foo returns 42\nR3: baz exists")
    assert r.rc == 0, r.err
    assert h.stages() == ["ptests", "pcover", "pbuild"]
    assert "requirements: 2, uncovered: ['R3']" in h.report()


def test_default_fix_rounds_is_four(h: PH) -> None:
    h.scenario({"pbuild": [{"base": True, "write": BAD}], "fix": [{"base": True, "write": BAD, "append": {"notes.txt": "x"}}]})
    h.run()
    assert h.stages().count("fix") == 4


def test_outage_in_build_pauses_without_consuming_a_round(h: PH) -> None:
    h.scenario({"pbuild": [{"base": True, "write": BAD}], "fix": [API, "default"]})
    r = h.run("Build foo", "--no-wait")
    assert r.rc == 3 and FINAL(r.out) == PAUSE
    st = h.state()
    assert st["stage"] == "plain-build" and st.get("plain_round", 0) == 0 and "status" not in st
    r2 = h.run("Build foo", "--no-wait")
    assert r2.rc == 0, r2.err
    assert h.stages() == ["ptests", "pbuild", "fix", "fix"]
    assert h.state()["plain_round"] == 1
    assert h.of("fix")[1]["resume"] == h.of("pbuild")[0]["session_id"]


def test_outage_in_first_turn_then_complete(h: PH) -> None:
    h.scenario({"ptests": [API, "default"]})
    assert h.run("Build foo", "--no-wait").rc == 3
    assert h.state()["stage"] == "plain-tests"
    assert h.run("Build foo", "--no-wait").rc == 0
    assert h.stages() == ["ptests", "ptests", "pbuild"]


@pytest.mark.parametrize("stage", ["ptests", "pbuild", "fix"])
def test_killed_driver_resumes_at_the_right_step(h: PH, stage: str) -> None:
    scn: dict = {stage: [{"kill_driver": True}, "default"]}
    if stage == "fix":
        scn["pbuild"] = [{"base": True, "write": BAD}]
    h.scenario(scn)
    h.run()
    st = h.state()
    assert st["engine"] == "plain" and st["stage"] == ("plain-tests" if stage == "ptests" else "plain-build")
    assert h.stages()[-1] == stage
    r = h.run()
    assert r.rc == 0, r.err
    assert FINAL(r.out) == "FORGE: done"
    expected = {"ptests": 1, "pbuild": 1, "fix": 1 if stage == "fix" else 0}
    expected[stage] += 1
    assert {s: h.stages().count(s) for s in expected} == expected
    first = h.of("ptests")[0]
    if stage == "pbuild":
        assert not h.of("pbuild")[1]["has_resume"] and first["session_id"]
        assert "previous attempt was interrupted" in h.of("pbuild")[1]["prompt"]
    if stage == "fix":
        assert h.of("fix")[1]["resume"] == h.of("pbuild")[0]["session_id"]
    if stage == "ptests":
        assert not h.of("ptests")[1]["has_resume"]


def test_finished_run_is_not_repeated(h: PH) -> None:
    h.run()
    n = len(h.calls())
    r = h.run()
    assert r.rc == 0 and FINAL(r.out) == "FORGE: done" and len(h.calls()) == n


def test_engine_forge_still_available(h: PH) -> None:
    r = Harness.run(h, "Build foo", "--engine", "forge")
    assert r.rc == 0, r.err
    assert h.stages() == ["audit", "tests", "implement", "accept"]
    assert h.state()["engine"] == "forge"


def test_accept_is_opt_in(h: PH) -> None:
    h.run("Build foo", "--accept")
    assert h.stages() == ["ptests", "pbuild", "accept"]
    assert h.of("accept")[0]["model"] == "strong" and not h.of("accept")[0]["has_resume"]


def hook(repo: Path, rel: str) -> int:
    event = {"cwd": str(repo), "tool_input": {"file_path": str(repo / rel)}}
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "hook_protect.py")], input=json.dumps(event), text=True,
                       capture_output=True)
    return p.returncode


def test_hook_protect_honours_frozen_json(tmp_path: Path) -> None:
    (tmp_path / "powerpuff.yaml").write_text(profile(), encoding="utf-8")
    (tmp_path / ".git").mkdir()
    assert hook(tmp_path, "tests/test_a.py") == 0
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "frozen.json").write_text(json.dumps({"files": {"tests/test_a.py": "x"}}), encoding="utf-8")
    assert hook(tmp_path, "tests/test_a.py") == 2
    assert hook(tmp_path, "tests/test_b.py") == 0


def queue_cmd(tmp: Path, mp: pytest.MonkeyPatch, args: tuple = (), **top: object) -> list[str]:
    qh = QH(tmp)
    seen: list[list[str]] = []
    orig = queue_run.Queue.forge_cmd

    def cmd(self, spec, wt, cap):
        seen.append(orig(self, spec, wt, cap))
        return [sys.executable, "-c", "import sys; sys.exit(7)"]
    mp.setattr(queue_run.Queue, "forge_cmd", cmd)
    for k, v in qh.env().items():
        mp.setenv(k, v)
    queue_run.main([str(qh.queue([unit("U1")], **top)), "--repo", str(qh.repo), *args])
    return seen[0]


@pytest.mark.parametrize("top,args,want", [
    ({"engine": None}, (), "plain"), ({"engine": "forge"}, (), "forge"), ({"engine": "plain"}, ("--engine", "forge"), "forge"),
    ({"engine": "forge"}, ("--engine", "plain"), "plain")])
def test_queue_passes_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, top: dict, args: tuple, want: str) -> None:
    cmd = queue_cmd(tmp_path, monkeypatch, args, **top)
    assert cmd[cmd.index("--engine") + 1] == want


def test_queue_rejects_unknown_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    qh = QH(tmp_path)
    for k, v in qh.env().items():
        monkeypatch.setenv(k, v)
    rc = queue_run.main([str(qh.queue([unit("U1")], engine="turbo")), "--repo", str(qh.repo)])
    assert rc == 3 and "engine must be one of" in capsys.readouterr().out
    assert os.environ.get("FAKE_STATE")
