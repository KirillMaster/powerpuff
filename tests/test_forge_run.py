"""Tests for scripts/forge_run.py driven end to end against tests/fake_claude.py (never the real claude CLI)."""

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DRIVER = ROOT / "scripts" / "forge_run.py"
FAKE = HERE / "fake_claude.py"
sys.path.insert(0, str(ROOT / "scripts"))
import forge_run  # noqa: E402

BAD = {"app.py": "def foo():\n    return 0\n\n\ndef bar():\n    return 0\n"}
GAP = json.dumps({"gaps": [{"id": "R1", "kind": "wrong", "evidence": "spec says 42, app.py:2 returns 0",
                            "fix": "return 42"}], "questions": []})
CORRECTED_TEST = ("from app import foo, bar\n\n\ndef test_r1():\n    # R1 corrected\n    assert foo() == 42\n\n\n"
                  "def test_r2():\n    # R2\n    assert bar() == 7\n")
EMPTY_GAPS = json.dumps({"gaps": [], "questions": []})
FINAL = lambda out: out.strip().splitlines()[-1]  # noqa: E731


class Res:
    def __init__(self, rc: int, out: str, err: str):
        self.rc, self.out, self.err = rc, out, err


class Harness:
    base_check = False  # True: the driver's green-base check runs (the sample repo is red on base by design)

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.bindir = tmp / "bin"
        self.bindir.mkdir()
        (self.bindir / "claude.cmd").write_text(f'@"{sys.executable}" "{FAKE}" %*\r\n', encoding="ascii")
        sh = self.bindir / "claude"
        sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="ascii")
        sh.chmod(sh.stat().st_mode | stat.S_IEXEC)
        self.scn = tmp / "scenario.json"
        self.pidfile = tmp / "driver.pid"
        self.repo = tmp / "repo"
        self.repo.mkdir()
        (self.repo / "app.py").write_text(BAD["app.py"], encoding="utf-8")
        (self.repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--no-gpg-sign", "-m", "init"]):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)
        self.scenario({})
        self.n = 0

    def scenario(self, stages: dict) -> None:
        self.scn.write_text(json.dumps({"stages": stages}), encoding="utf-8")

    def run(self, spec: str, *args: str, cwd: Path | None = None, timeout: int = 120) -> Res:
        self.n += 1
        env = {**os.environ, "PATH": str(self.bindir) + os.pathsep + os.environ["PATH"],
               "FAKE_CLAUDE_SCENARIO": str(self.scn), "FAKE_CLAUDE_PIDFILE": str(self.pidfile),
               "PYTHONUNBUFFERED": "1"}
        cmd = [sys.executable, str(DRIVER), spec, "--repo", str(self.repo), "--engine", "forge", "--model", "strong",
               "--writer", "writer", "--max-hours", "0.5", *([] if self.base_check else ["--skip-base-check"]), *args]
        out_p, err_p = self.tmp / f"out{self.n}.txt", self.tmp / f"err{self.n}.txt"
        with open(out_p, "w", encoding="utf-8") as so, open(err_p, "w", encoding="utf-8") as se:
            p = subprocess.Popen(cmd, cwd=cwd or self.tmp, env=env, stdout=so, stderr=se)
            self.pidfile.write_text(str(p.pid))
            try:
                rc = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                raise
        return Res(rc, out_p.read_text(encoding="utf-8"), err_p.read_text(encoding="utf-8"))

    def calls(self) -> list[dict]:
        p = Path(str(self.scn) + ".calls.jsonl")
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.is_file() else []

    def stages(self) -> list[str]:
        return [c["stage"] for c in self.calls()]

    def state(self) -> dict:
        return json.loads((self.repo / ".forge" / "state.json").read_text(encoding="utf-8"))

    def report(self) -> str:
        return (self.repo / ".forge" / "REPORT.md").read_text(encoding="utf-8")


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_fake_is_what_runs(h: Harness) -> None:
    h.run("Build foo", "--preflight")
    calls = h.calls()
    assert [c["stage"] for c in calls] == ["audit"]
    assert calls[0]["model"] == "strong"
    assert calls[0]["prompt"].startswith("You are the spec auditor")


def test_happy_path(h: Harness) -> None:
    r = h.run("Build foo returning 42 and bar returning 7")
    assert r.rc == 0, r.err
    assert FINAL(r.out) == "FORGE: done"
    assert h.stages() == ["audit", "tests", "implement", "accept"]
    assert {c["stage"]: c["model"] for c in h.calls()} == {
        "audit": "strong", "tests": "writer", "implement": "writer", "accept": "strong"}
    assert not any(c["has_resume"] for c in h.calls())
    st = h.state()
    assert (st["stage"], st["status"]) == ("finished", "done")
    cost = st["cost"]
    assert cost["total_usd"] == pytest.approx(1.0)
    assert cost["sessions"] == 4 and cost["turns"] == 12
    assert set(cost["by_stage"]) == {"audit", "tests", "build1", "accept1"}
    assert cost["by_model"] == {"strong": pytest.approx(0.5), "writer": pytest.approx(0.5)}
    rep = h.report()
    assert "FORGE REPORT - DONE" in rep and "requirements: 2" in rep and "unit=pass" in rep
    assert "cost (notional USD): 1.0" in rep
    assert "D1: rounding" in rep and "Q1:" in rep
    assert "tests/test_acceptance.py" in (h.repo / ".forge" / "frozen.sha256").read_text(encoding="utf-8")
    assert "return 42" in (h.repo / "app.py").read_text(encoding="utf-8")


def test_spec_dir_concatenated(h: Harness) -> None:
    d = h.tmp / "specdir"
    d.mkdir()
    (d / "tasks.md").write_text("TASKS B", encoding="utf-8")
    (d / "spec.md").write_text("SPEC A", encoding="utf-8")
    r = h.run(str(d), "--preflight")
    assert FINAL(r.out) == "FORGE: preflight"
    text = (h.repo / ".forge" / "spec.md").read_text(encoding="utf-8")
    assert text == "# spec.md\n\nSPEC A\n\n# tasks.md\n\nTASKS B"


def test_spec_single_file(h: Harness) -> None:
    f = h.tmp / "ticket.md"
    f.write_text("Ticket body\nline two", encoding="utf-8")
    h.run(str(f), "--preflight")
    assert (h.repo / ".forge" / "spec.md").read_text(encoding="utf-8") == "Ticket body\nline two"


def test_spec_inline_text(h: Harness) -> None:
    h.run("Build foo\nwith two lines", "--preflight")
    assert (h.repo / ".forge" / "spec.md").read_text(encoding="utf-8") == "Build foo\nwith two lines"


@pytest.mark.parametrize("spec", ["   ", ""])
def test_empty_spec_fails(h: Harness, spec: str) -> None:
    cwd = h.tmp / "cwd_with_spec"
    cwd.mkdir()
    (cwd / "spec.md").write_text("must not be picked up for an empty argument", encoding="utf-8")
    r = h.run(spec, cwd=cwd)
    assert r.rc == 1
    assert FINAL(r.out) == "FORGE: failed"
    assert "empty spec" in h.report()
    assert h.calls() == []


def test_audit_nothing_written_retried_then_failed(h: Harness) -> None:
    h.scenario({"audit": [{}]})
    r = h.run("Build foo")
    assert r.rc == 1
    assert FINAL(r.out) == "FORGE: failed"
    calls = h.calls()
    assert [c["stage"] for c in calls] == ["audit", "retry"]
    assert calls[1]["has_resume"] and calls[1]["resume"] == calls[0]["session_id"]
    assert calls[1]["model"] == "strong"
    assert "audit produced no requirements/gates" in h.report()


def test_preflight_then_continue(h: Harness) -> None:
    r1 = h.run("Build foo", "--preflight")
    assert r1.rc == 0 and FINAL(r1.out) == "FORGE: preflight"
    assert h.stages() == ["audit"]
    assert h.state()["stage"] == "tests"
    r2 = h.run("Build foo")
    assert r2.rc == 0 and FINAL(r2.out) == "FORGE: done"
    assert h.stages() == ["audit", "tests", "implement", "accept"]


def test_uncovered_requirement_triggers_topup(h: Harness) -> None:
    h.scenario({
        "audit": [{"base": True, "write": {".forge/requirements.md": "R1: a\nR2: b\nR3: c\n"}}],
        "tests": [{"base": True, "write": {"tests/test_acceptance.py": "# R1\n# R2\n"}}]})
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    assert FINAL(r.out) == "FORGE: done"
    assert h.stages() == ["audit", "tests", "topup", "implement", "accept"]
    top = h.calls()[2]
    assert top["model"] == "writer" and "R3" in top["prompt"] and "R1" not in top["prompt"].split("UNCOVERED")[0]
    frozen = (h.repo / ".forge" / "frozen.sha256").read_text(encoding="utf-8")
    assert "tests/test_acceptance_extra.py" in frozen and "tests/test_acceptance.py" in frozen
    assert "uncovered: []" in h.report()


def test_build_red_then_green_resumes_same_session(h: Harness) -> None:
    h.scenario({"implement": [{"base": True, "write": BAD}]})
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    assert h.stages() == ["audit", "tests", "implement", "fix", "accept"]
    impl, fix = h.calls()[2], h.calls()[3]
    assert not impl["has_resume"]
    assert fix["has_resume"] and fix["resume"] == impl["session_id"] and fix["model"] == "writer"
    rep = json.loads(fix["prompt"].split("forge_check output:\n", 1)[1])
    assert rep["done"] is False and rep["gates"]["results"]["unit"]["ok"] is False
    assert rep["coverage"]["ok"] is True and rep["frozen"]["ok"] is True


def test_build_edits_frozen_test_is_restored(h: Harness) -> None:
    h.scenario({"implement": [{"base": True, "append": {"tests/test_acceptance.py": "\n# tampered\n"}}]})
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    assert h.stages() == ["audit", "tests", "implement", "fix", "accept"]
    fix = h.calls()[3]
    rep = json.loads(fix["prompt"].split("forge_check output:\n", 1)[1])
    assert rep["done"] is False and rep["gates"]["ok"] is True
    assert rep["frozen"]["ok"] is False
    assert rep["frozen"]["changed_or_missing"] == ["tests/test_acceptance.py"]
    assert "restored from git" in rep["frozen"]["note"]
    assert "tampered" not in (h.repo / "tests" / "test_acceptance.py").read_text(encoding="utf-8")


def test_build_red_three_rounds_then_escalation_green(h: Harness) -> None:
    h.scenario({"implement": [{"base": True, "write": BAD}, "default"], "fix": [{"base": True, "write": BAD}]})
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    assert FINAL(r.out) == "FORGE: done"
    assert h.stages() == ["audit", "tests", "implement", "fix", "fix", "implement", "accept"]
    esc = h.calls()[5]
    assert esc["model"] == "strong" and not esc["has_resume"]
    assert "A previous implementer left this failing" in esc["prompt"]
    assert [c["model"] for c in h.calls()[2:5]] == ["writer"] * 3


def test_build_red_everywhere_fails(h: Harness) -> None:
    h.scenario({"implement": [{"base": True, "write": BAD}], "fix": [{"base": True, "write": BAD}]})
    r = h.run("Build foo")
    assert r.rc == 1
    assert FINAL(r.out) == "FORGE: failed"
    assert h.stages().count("implement") == 2 and h.stages().count("fix") == 2
    assert h.state()["build_green"] is False
    rep = h.report()
    assert "FAILED" in rep and "unit=FAIL" in rep and "forge_check is red" in rep


def test_acceptance_gap_then_gapfix_then_clean(h: Harness) -> None:
    h.scenario({"accept": [{"write": {".forge/gaps.json": GAP}}, "default"]})
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    assert h.stages() == ["audit", "tests", "implement", "accept", "gapfix", "accept"]
    gapfix = h.calls()[4]
    assert gapfix["model"] == "writer" and "spec says 42" in gapfix["prompt"]
    assert h.state()["open_gaps"] == []
    assert "FORGE REPORT - DONE" in h.report()


def test_acceptance_gaps_every_round_fail(h: Harness) -> None:
    h.scenario({"accept": [{"write": {".forge/gaps.json": GAP}}]})
    r = h.run("Build foo")
    assert r.rc == 1
    assert FINAL(r.out) == "FORGE: failed"
    assert h.stages() == ["audit", "tests", "implement", "accept", "gapfix", "accept", "gapfix", "accept"]
    rep = h.report()
    assert "open acceptance gap" in rep
    assert "spec says 42, app.py:2 returns 0" in rep.split("## Open acceptance gaps")[1]


def test_reviewer_corrects_frozen_test_refreezes(h: Harness) -> None:
    h.scenario({"accept": [{"write": {"tests/test_acceptance.py": CORRECTED_TEST,
                                      ".forge/unfrozen.md": "- tests/test_acceptance.py: wrong\n",
                                      ".forge/gaps.json": EMPTY_GAPS}}]})
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    assert FINAL(r.out) == "FORGE: done"
    frozen = (h.repo / ".forge" / "frozen.sha256").read_text(encoding="utf-8")
    digest = hashlib.sha256((h.repo / "tests" / "test_acceptance.py").read_bytes()).hexdigest()
    assert f"{digest}  tests/test_acceptance.py" in frozen
    assert "R1 corrected" in (h.repo / "tests" / "test_acceptance.py").read_text(encoding="utf-8")
    assert "wrong" in h.report().split("## Disputed")[1]


def test_reviewer_edit_without_unfrozen_note_fails(h: Harness) -> None:
    h.scenario({"accept": [{"write": {"tests/test_acceptance.py": CORRECTED_TEST, ".forge/gaps.json": EMPTY_GAPS}}]})
    r = h.run("Build foo")
    assert r.rc == 1
    assert FINAL(r.out) == "FORGE: failed"
    assert "R1 corrected" not in (h.repo / "tests" / "test_acceptance.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("stage,state_stage", [("audit", "audit"), ("tests", "tests"), ("implement", "build"),
                                               ("accept", "accept")])
def test_killed_driver_resumes_without_repeating_finished_stages(h: Harness, stage: str, state_stage: str) -> None:
    h.scenario({stage: [{"kill_driver": True}, "default"]})
    r1 = h.run("Build foo")
    assert not r1.out.strip().endswith(("FORGE: done", "FORGE: failed"))
    st = h.state()
    assert st["stage"] == state_stage
    before = h.stages()
    assert before[-1] == stage
    r2 = h.run("Build foo")
    assert r2.rc == 0, r2.err
    assert FINAL(r2.out) == "FORGE: done"
    expected = {"audit": 1, "tests": 1, "implement": 1, "accept": 1}
    expected[stage] = 2
    got = {s: h.stages().count(s) for s in expected}
    assert got == expected
    assert h.state()["status"] == "done"


def test_killed_build_restarts_with_fresh_session(h: Harness) -> None:
    h.scenario({"implement": [{"kill_driver": True}, "default"]})
    h.run("Build foo")
    h.run("Build foo")
    second = [c for c in h.calls() if c["stage"] == "implement"][1]
    assert not second["has_resume"]
    assert "A previous session was interrupted" not in second["prompt"]


def test_session_without_result_event_is_retried(h: Harness) -> None:
    h.scenario({"audit": [{"base": True, "result": "no_result", "exit_code": 1}, "default"]})
    r = h.run("Build foo", "--preflight")
    assert FINAL(r.out) == "FORGE: preflight"
    assert h.stages() == ["audit", "audit"]
    assert h.state()["cost"]["total_usd"] == pytest.approx(0.25)


def test_is_error_result_is_retried(h: Harness) -> None:
    h.scenario({"tests": [{"result": "is_error", "text": "boom"}, "default"]})
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    assert h.stages() == ["audit", "tests", "tests", "implement", "accept"]


def test_rate_limit_no_wait_stream_exits_3_and_next_run_continues(h: Harness) -> None:
    epoch = int(time.time()) + 7200
    h.scenario({"tests": [{"result": "rate_limit", "epoch": epoch}, "default"]})
    r1 = h.run("Build foo", "--stream", "--no-wait")
    assert r1.rc == 3
    lines = [json.loads(x) for x in r1.out.splitlines() if x.strip()]
    results = [e for e in lines if e.get("type") == "result"]
    assert len(results) == 1 and lines[-1] is results[0]
    assert results[0]["is_error"] is True and "rate limit" in results[0]["result"]
    assert str(epoch) in results[0]["result"]
    assert h.state()["stage"] == "tests"
    assert h.stages() == ["audit", "tests"]
    r2 = h.run("Build foo", "--stream", "--no-wait")
    assert r2.rc == 0, r2.err
    assert h.stages() == ["audit", "tests", "tests", "implement", "accept"]
    assert "FORGE: done" in json.loads(r2.out.strip().splitlines()[-1])["result"]


def test_rate_limit_no_wait_without_stream_prints_nothing_final(h: Harness) -> None:
    h.scenario({"audit": [{"result": "rate_limit"}, "default"]})
    r = h.run("Build foo", "--no-wait")
    assert r.rc == 3
    assert "FORGE:" not in r.out


def test_rate_limit_sleeps_until_deadline(h: Harness) -> None:
    h.scenario({"audit": [{"result": "rate_limit", "epoch": int(time.time()) + 600}]})
    t0 = time.time()
    r = h.run("Build foo", "--max-hours", "0.0015")
    assert time.time() - t0 < 60
    assert r.rc == 1 and FINAL(r.out) == "FORGE: failed"
    assert "wall-clock budget exhausted" in h.report()
    assert "rate limited, sleeping" in (h.repo / ".forge" / "progress.md").read_text(encoding="utf-8")


def test_staged_sleeps_on_rate_limit_without_consuming_try(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a = argparse.Namespace(repo=str(tmp_path), max_hours=1, max_usd=60, no_wait=False, stream=False,
                           permission_mode="x")
    run = forge_run.Run(a)
    seq: list = [forge_run.RateLimited(f"limit|{int(time.time()) + 300}"),
                 {"ok": True, "text": "", "session_id": "s"}]
    calls: list[str] = []

    def fake_session(self, stage, model, prompt, resume=None):  # noqa: ANN001
        calls.append(stage)
        x = seq.pop(0)
        if isinstance(x, Exception):
            raise x
        return x

    slept: list[float] = []
    monkeypatch.setattr(forge_run.Run, "session", fake_session)
    monkeypatch.setattr(forge_run.time, "sleep", slept.append)
    out = run.staged("s", "m", "p", lambda: True, tries=1)
    assert out["ok"] and len(calls) == 2
    assert len(slept) == 1 and 300 <= slept[0] <= 380


def test_stream_output_is_json_lines_with_one_final_result(h: Harness) -> None:
    r = h.run("Build foo", "--stream")
    assert r.rc == 0, r.err
    events = [json.loads(x) for x in r.out.splitlines()]
    results = [e for e in events if e.get("type") == "result"]
    assert len(results) == 1 and events[-1] is results[0]
    assert any(e.get("type") == "assistant" for e in events)
    res = results[0]
    assert "FORGE: done" in res["result"] and res["is_error"] is False
    cost = h.state()["cost"]
    assert res["total_cost_usd"] == cost["total_usd"] == pytest.approx(1.0)
    assert res["modelUsage"] == {"strong": {"costUSD": pytest.approx(0.5)}, "writer": {"costUSD": pytest.approx(0.5)}}
    assert "FORGE:" in r.err


def test_cost_budget_exhausted(h: Harness) -> None:
    r = h.run("Build foo", "--max-usd", "0.5")
    assert r.rc == 1
    assert FINAL(r.out) == "FORGE: failed"
    assert h.stages() == ["audit", "tests"]
    assert "cost budget exhausted" in h.report()


def test_wall_clock_budget_zero(h: Harness) -> None:
    r = h.run("Build foo", "--max-hours", "0")
    assert r.rc == 1
    assert FINAL(r.out) == "FORGE: failed"
    assert h.calls() == []
    assert "wall-clock budget exhausted" in h.report()


@pytest.mark.parametrize("fail", [False, True])
def test_finished_state_reruns_without_calls(h: Harness, fail: bool) -> None:
    if fail:
        h.scenario({"accept": [{"write": {".forge/gaps.json": GAP}}]})
    h.run("Build foo")
    n = len(h.calls())
    r = h.run("Build foo")
    assert len(h.calls()) == n
    assert FINAL(r.out) == ("FORGE: failed" if fail else "FORGE: done")
    assert r.rc == (1 if fail else 0)


def test_reset_wait_epoch() -> None:
    w = forge_run.reset_wait_s(f"Claude AI usage limit reached|{int(time.time()) + 3600}")
    assert 3600 + 60 - 5 <= w <= 3600 + 60 + 1


def test_reset_wait_epoch_in_past_is_at_least_a_minute() -> None:
    assert forge_run.reset_wait_s(f"limit|{int(time.time()) - 5000}") == 60.0


@pytest.mark.parametrize("text,hour,minute", [("limit reached, resets at 3pm", 15, 0),
                                              ("resets 11:30 pm", 23, 30),
                                              ("Resets at 12am", 0, 0),
                                              ("resets at 12:15 PM", 12, 15)])
def test_reset_wait_clock_form(text: str, hour: int, minute: int) -> None:
    local = datetime.now().astimezone()
    at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at <= local:
        at += timedelta(days=1)
    expected = (at - local).total_seconds() + 60
    assert forge_run.reset_wait_s(text) == pytest.approx(expected, abs=5)


def test_reset_wait_fallback() -> None:
    assert forge_run.reset_wait_s("something unrelated") == 900
    assert forge_run.reset_wait_s("something unrelated", fallback_min=5) == 300
