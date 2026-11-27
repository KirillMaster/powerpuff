"""Claude Code advisor (`advisorModel`) in plain build/fix sessions, prepare sessions and the queue."""

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))
import forge_run  # noqa: E402
import queue_run  # noqa: E402
from test_forge_profile import profile  # noqa: E402
from test_forge_run import BAD  # noqa: E402
from test_plain_engine import PH  # noqa: E402
from test_prepare_run import Harness as PrepH  # noqa: E402


@pytest.fixture
def h(tmp_path: Path) -> PH:
    return PH(tmp_path)


def progress(h: PH) -> str:
    return (h.repo / ".forge" / "progress.md").read_text(encoding="utf-8")


def test_profile_advisor_reaches_build_and_fix_only(tmp_path: Path) -> None:
    h = PH(tmp_path, profile(extra="test_gates: [unit]\nmodel_build: sonnet\nadvisor: fable\n"))
    h.scenario({"pbuild": [{"base": True, "write": BAD}], "fix": ["default"]})
    r = h.run()
    assert r.rc == 0, r.err
    assert h.of("ptests")[0]["advisor"] is None
    assert h.of("pbuild")[0]["advisor"] == "fable" and h.of("fix")[0]["advisor"] == "fable"
    assert "Consult it before committing" in h.of("pbuild")[0]["prompt"]
    assert "Consult it before committing" not in h.of("ptests")[0]["prompt"]
    assert h.state()["advisor"] == {"model": "fable", "calls": 2, "errors": 0}
    assert "advisor_calls=1" in progress(h) and "never called" not in progress(h)


def test_no_advisor_by_default(h: PH) -> None:
    assert h.run().rc == 0
    assert {c["advisor"] for c in h.calls()} == {None}
    assert "advisor" not in h.state()


def test_cli_off_beats_profile(tmp_path: Path) -> None:
    h = PH(tmp_path, profile(extra="test_gates: [unit]\nadvisor: fable\n"))
    assert h.run("Build foo", "--advisor", "off").rc == 0
    assert {c["advisor"] for c in h.calls()} == {None}


def test_unavailable_advisor_warns_and_continues(h: PH) -> None:
    h.scenario({"pbuild": [{"base": True, "advisor": "error"}]})
    r = h.run("Build foo", "--advisor", "fable")
    assert r.rc == 0, r.err
    assert "WARNING advisor unavailable (unavailable), continuing without it" in progress(h)
    assert h.state()["advisor"]["errors"] == 1


def test_never_called_advisor_is_reported(h: PH) -> None:
    h.scenario({"pbuild": [{"base": True, "advisor": "silent"}]})
    assert h.run("Build foo", "--advisor", "fable").rc == 0
    assert "WARNING advisor fable was configured but never called" in progress(h)


def test_env_default_applies_without_profile_key(h: PH, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POWERPUFF_ADVISOR", "fable")
    assert h.run().rc == 0
    assert h.of("pbuild")[0]["advisor"] == "fable" and h.of("ptests")[0]["advisor"] is None


def test_profile_off_beats_env_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POWERPUFF_ADVISOR", "fable")
    h = PH(tmp_path, profile(extra="test_gates: [unit]\nadvisor: off\n"))
    assert h.run().rc == 0
    assert {c["advisor"] for c in h.calls()} == {None}


def test_queue_passes_advisor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_plain_engine import queue_cmd
    cmd = queue_cmd(tmp_path, monkeypatch, (), advisor="fable")
    assert cmd[cmd.index("--advisor") + 1] == "fable"
    (tmp_path / "b").mkdir()
    assert "--advisor" not in queue_cmd(tmp_path / "b", monkeypatch)


def test_prepare_sessions_use_the_advisor_and_the_queue_keeps_it(tmp_path: Path) -> None:
    h = PrepH(tmp_path)
    r = h.run(None, "--assume-defaults", "--advisor", "fable")
    assert r.rc == 0, r.out + r.err
    assert {c["advisor"] for c in h.calls()} == {"fable"}
    cfg = queue_run.normalize(queue_run.load_queue(h.out), h.out, h.repo)[0]
    assert cfg["advisor"] == "fable"


def test_advisor_event_counts_calls_and_errors() -> None:
    t: dict = {}
    forge_run.advisor_event({"type": "assistant", "message": {"content": [
        {"type": "server_tool_use", "name": "advisor"},
        {"type": "advisor_tool_result", "content": {"type": "advisor_tool_result_error", "error_code": "overloaded"}},
        {"type": "advisor_tool_result", "content": {"type": "advisor_redacted_result"}}]}}, t)
    assert t == {"calls": 1, "errors": 1, "last_error": "overloaded"}
    assert forge_run.advisor_settings("off", Path(".")) == [] and forge_run.advisor_settings(None, Path(".")) == []
