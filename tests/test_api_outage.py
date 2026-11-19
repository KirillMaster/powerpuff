"""An unreachable API (network down, 5xx) is a transient condition: wait with backoff, never a verdict on the code."""

import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import forge_run  # noqa: E402
import prepare_run  # noqa: E402
import queue_run  # noqa: E402
from test_forge_run import FINAL, Harness as FH  # noqa: E402
from test_prepare_run import GOOD, Harness as PH  # noqa: E402
from test_queue_run import FAKE_SRC as QUEUE_FAKE, QH, unit  # noqa: E402

API = {"result": "api_error"}
API_TEXT = "API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)"
PAUSE = "FORGE: paused (API unreachable)"


@pytest.fixture
def fh(tmp_path: Path) -> FH:
    return FH(tmp_path)


def forge_inproc(h: FH, mp: pytest.MonkeyPatch, *args: str) -> tuple[int, list[float]]:
    mp.setenv("PATH", str(h.bindir) + os.pathsep + os.environ["PATH"])
    mp.setenv("FAKE_CLAUDE_SCENARIO", str(h.scn))
    mp.chdir(h.tmp)
    slept: list[float] = []
    mp.setattr(forge_run.time, "sleep", slept.append)
    mp.setattr(sys, "argv", ["forge_run.py", "Build foo", "--repo", str(h.repo), "--engine", "forge", "--model", "strong",
                             "--writer", "writer", "--max-hours", "0.5", "--skip-base-check", *args])
    try:
        rc = forge_run.main()
    except SystemExit as e:
        rc = int(e.code or 0)
    return rc, slept


def test_failure_kind_classification() -> None:
    kind = forge_run.failure_kind
    structured = {"is_error": True, "api_error": "timeout", "result": "x"}
    assert kind(structured, "x", []) == "api"
    assert kind({"is_error": True, "terminal_reason": "api_error", "result": ""}, "", []) == "api"
    assert kind({"is_error": True, "result": ""}, "", [], api_msg=True) == "api"
    assert kind({"is_error": True, "result": API_TEXT}, API_TEXT, []) == "api"
    assert kind({"is_error": True, "result": "API Error: 503 service unavailable"}, "API Error: 503", []) == "api"
    assert kind(None, "", ["Error: getaddrinfo EAI_AGAIN api.anthropic.com"]) == "api"
    assert kind({"is_error": True, "api_error": "x", "result": "429 overloaded"}, "429 overloaded", []) == "rate"
    assert kind({"is_error": True, "result": "boom"}, "boom", []) is None
    assert kind(None, "", []) is None
    assert kind({"is_error": False, "result": "fixed ENOTFOUND handling"}, "fixed ENOTFOUND handling", []) is None


def test_backoff_steps_and_limits() -> None:
    assert [forge_run.backoff_s(n) for n in (1, 2, 3, 4, 5, 6, 7, 8)] == [30, 60, 120, 240, 480, 900, 900, 900]
    o = forge_run.Outage(1)
    assert [o.next_wait(), o.next_wait(), o.next_wait()] == [30, 30, None]
    assert forge_run.Outage(120).next_wait(remaining_s=10) == 10
    assert forge_run.Outage(120).next_wait(remaining_s=0) is None


def test_forge_transient_errors_do_not_consume_tries(fh: FH, monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture) -> None:
    fh.scenario({"audit": [API, API, API, "default"]})
    rc, slept = forge_inproc(fh, monkeypatch, "--max-outage-min", "30")
    out = capsys.readouterr().out
    assert rc == 0 and FINAL(out) == "FORGE: done", out
    assert slept == [30, 60, 120]
    assert fh.stages() == ["audit"] * 4 + ["tests", "implement", "accept"]
    progress = (fh.repo / ".forge" / "progress.md").read_text(encoding="utf-8")
    assert "audit: API unreachable, retry in 30 s (attempt 1)" in progress
    assert "audit: API unreachable, retry in 120 s (attempt 3)" in progress
    assert fh.state()["cost"]["total_usd"] == pytest.approx(1.0)


def test_forge_outage_over_the_limit_pauses_and_rerun_completes(fh: FH, monkeypatch: pytest.MonkeyPatch,
                                                                capsys: pytest.CaptureFixture) -> None:
    fh.scenario({"tests": [API]})
    rc, slept = forge_inproc(fh, monkeypatch, "--max-outage-min", "1")
    out = capsys.readouterr().out
    assert rc == 3 and FINAL(out) == PAUSE
    assert slept == [30, 30]
    assert fh.state()["stage"] == "tests" and "status" not in fh.state()
    assert fh.stages() == ["audit", "tests", "tests", "tests"]
    assert "NOT a verdict about the code" in fh.report()
    fh.scenario({})
    rc, slept = forge_inproc(fh, monkeypatch)
    assert rc == 0 and slept == [] and fh.state()["status"] == "done"
    assert fh.stages()[-3:] == ["tests", "implement", "accept"]


def test_forge_no_wait_pauses_at_once_with_stream_marker(fh: FH) -> None:
    fh.scenario({"tests": [API, "default"]})
    r1 = fh.run("Build foo", "--stream", "--no-wait")
    assert r1.rc == 3
    results = [e for e in (json.loads(x) for x in r1.out.splitlines() if x.strip()) if e.get("type") == "result"]
    assert len(results) == 1 and json.loads(r1.out.strip().splitlines()[-1]) == results[0]
    assert results[0]["is_error"] is True and results[0]["forge_outcome"] == "paused-api"
    assert PAUSE in results[0]["result"]
    assert fh.state()["stage"] == "tests" and fh.stages() == ["audit", "tests"]
    r2 = fh.run("Build foo", "--stream", "--no-wait")
    assert r2.rc == 0, r2.err
    assert "forge_outcome" not in json.loads(r2.out.strip().splitlines()[-1])
    assert fh.stages() == ["audit", "tests", "tests", "implement", "accept"]


def test_forge_no_wait_without_stream_prints_pause_line(fh: FH) -> None:
    fh.scenario({"audit": [API, "default"]})
    r = fh.run("Build foo", "--no-wait")
    assert r.rc == 3 and FINAL(r.out) == PAUSE


def test_forge_ordinary_error_still_consumes_tries_and_fails(fh: FH) -> None:
    fh.scenario({"audit": [{"result": "is_error", "text": "boom"}]})
    r = fh.run("Build foo")
    assert r.rc == 1 and FINAL(r.out) == "FORGE: failed"
    assert fh.stages() == ["audit", "audit"]


def test_forge_rate_limit_still_wins(fh: FH) -> None:
    fh.scenario({"audit": [{"result": "rate_limit"}, "default"]})
    r = fh.run("Build foo", "--stream", "--no-wait")
    last = json.loads(r.out.strip().splitlines()[-1])
    assert r.rc == 3 and "rate limit" in last["result"] and "forge_outcome" not in last


@pytest.fixture
def ph(tmp_path: Path) -> PH:
    return PH(tmp_path)


def prepare_inproc(h: PH, mp: pytest.MonkeyPatch, *args: str) -> tuple[int, list[float]]:
    mp.setenv("PATH", str(h.bindir) + os.pathsep + os.environ["PATH"])
    mp.setenv("FAKE_CLAUDE_SCENARIO", str(h.scn))
    mp.chdir(h.tmp)
    slept: list[float] = []
    mp.setattr(prepare_run.time, "sleep", slept.append)
    rc = prepare_run.main([h.spec, "--repo", str(h.repo), "--model", "strong", "--out", str(h.out), *args])
    return rc, slept


def test_prepare_transient_then_success(ph: PH, monkeypatch: pytest.MonkeyPatch) -> None:
    err = {"result": "is_error", "text": API_TEXT}
    ph.scenario(slice=[err, err, {"text": GOOD}])
    rc, slept = prepare_inproc(ph, monkeypatch, "--assume-defaults")
    assert rc == 0 and slept == [30, 60]
    assert ph.stages() == ["questions", "slice", "slice", "slice"]


def test_prepare_no_wait_exits_3_and_resumes(ph: PH) -> None:
    ph.scenario(slice=[{"result": "is_error", "text": API_TEXT}, {"text": GOOD}])
    r = ph.run(None, "--assume-defaults", "--no-wait")
    assert r.rc == 3 and r.last == "PREPARE: paused (API unreachable, rerun to resume)"
    assert ph.stages() == ["questions", "slice"] and not ph.out.exists()
    r = ph.run(None, "--assume-defaults", "--no-wait")
    assert r.rc == 0 and ph.stages() == ["questions", "slice", "slice"]


def api_queue_harness(tmp: Path) -> QH:
    src = QUEUE_FAKE
    for old, new in (
            ('    if "rate_once" in dirs and once("rate"):', '    if "api_once" in dirs and once("api"):\n'
             '        kind, cost = "api", 0\n    elif "rate_once" in dirs and once("rate"):'),
            ('is_error = kind == "rate_limit"', 'is_error = kind in ("rate_limit", "api")'),
            ('text = f"Claude AI usage limit reached|{int(time.time()) + 5}" if is_error else "done"',
             'text = "API Error: Can\'t reach the API server (ENOTFOUND)" if kind == "api" else '
             'f"Claude AI usage limit reached|{int(time.time()) + 5}" if is_error else "done"'),
            ('"modelUsage": {model: {"costUSD": cost}}}), flush=True)',
             '"modelUsage": {model: {"costUSD": cost}}, **({"api_error": "connection_failed", '
             '"terminal_reason": "api_error"} if kind == "api" else {})}), flush=True)')):
        assert old in src
        src = src.replace(old, new)
    qh = QH(tmp)
    (tmp / "fake_q.py").write_text(src, encoding="utf-8")
    return qh


def test_queue_api_pause_is_not_a_failure_and_resumes(tmp_path: Path) -> None:
    qh = api_queue_harness(tmp_path)
    q = qh.queue([unit("U1", fake=("api_once",)), unit("U2")])
    r = qh.run(q, "--no-wait")
    assert r.rc == 3, r.out
    assert qh.statuses() == {"U1": "running", "U2": "pending"}
    assert qh.state()["units"]["U1"]["reason"] == "API unreachable"
    assert qh.stages("U2") == []
    r2 = qh.run(q, "--no-wait")
    assert r2.rc == 0 and r2.last == "QUEUE: done", r2.out
    assert qh.statuses() == {"U1": "merged", "U2": "merged"}
    assert qh.stages("U1").count("audit") == 1


def test_queue_waits_with_backoff_then_completes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture) -> None:
    qh = api_queue_harness(tmp_path)
    slept: list[float] = []
    monkeypatch.setattr(queue_run, "sleep", slept.append)
    for k, v in qh.env().items():
        monkeypatch.setenv(k, v)
    rc = queue_run.main([str(qh.queue([unit("U1", fake=("api_once",))])), "--repo", str(qh.repo)])
    assert rc == 0, capsys.readouterr().out
    assert slept == [30]
    assert qh.statuses() == {"U1": "merged"}
