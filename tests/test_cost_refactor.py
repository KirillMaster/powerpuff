"""0.3.0 cost/speed refactor: rate-limit events, build/fix models and sessions, stuck fix rounds, spec slices,
queue retries / already_merged / parallel units / rate throttle."""

import json
import os
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts"))
import queue_run  # noqa: E402
import spec_slice  # noqa: E402
from test_forge_profile import profile  # noqa: E402
from test_forge_run import BAD, FINAL  # noqa: E402
from test_plain_engine import PH, PROFILE  # noqa: E402
from test_queue_run import QH, unit  # noqa: E402

RATE = {"result": "rate_event"}


@pytest.fixture
def h(tmp_path: Path) -> PH:
    return PH(tmp_path)


# ---- forge_run: rate limits -------------------------------------------------------------------------
def test_rejected_rate_event_is_a_rate_limit_not_an_outage(h: PH) -> None:
    epoch = int(time.time()) + 7200
    h.scenario({"pbuild": [{"result": "rate_event", "epoch": epoch}, "default"]})
    r = h.run("Build foo", "--no-wait", "--stream")
    assert r.rc == 3
    res = [json.loads(x) for x in r.out.splitlines() if x.startswith("{")][-1]
    assert res["is_error"] and f"|{epoch}" in res["result"] and "paused-api" not in json.dumps(res)
    st = h.state()
    assert st["stage"] == "plain-build" and "status" not in st
    assert "API unreachable" not in (h.repo / ".forge" / "progress.md").read_text(encoding="utf-8")
    r2 = h.run("Build foo", "--no-wait")
    assert r2.rc == 0, r2.err
    assert h.stages() == ["ptests", "pbuild", "pbuild"]


def test_rate_wait_comes_from_resets_at() -> None:
    import forge_run
    epoch = int(time.time()) + 3600
    assert 3600 <= forge_run.reset_wait_s(f"x\nrate limit rejected, resets |{epoch}") <= 3700


def test_rate_events_are_written_for_the_caller(h: PH, tmp_path: Path) -> None:
    rate = tmp_path / "rate.json"
    h.scenario({"ptests": [{"base": True, "result": "rate_warning"}]})
    os.environ["POWERPUFF_RATE_FILE"] = str(rate)
    try:
        assert h.run().rc == 0
    finally:
        del os.environ["POWERPUFF_RATE_FILE"]
    data = json.loads(rate.read_text(encoding="utf-8"))
    assert data["status"] == "allowed_warning" and data["utilization"] == 0.8


# ---- forge_run: build / fix sessions and models -----------------------------------------------------
def test_build_is_a_fresh_session_with_the_spec_inline(h: PH) -> None:
    assert h.run("Build foo exactly").rc == 0
    t1, t2 = h.of("ptests")[0], h.of("pbuild")[0]
    assert not t2["has_resume"] and "Build foo exactly" in t2["prompt"] and "tests/test_acceptance.py" in t2["prompt"]
    assert t1["model"] == t2["model"] == "strong"


def test_model_build_and_fix_with_handoff_and_escalation(h: PH) -> None:
    h.scenario({"pbuild": [{"base": True, "write": BAD}],
                "fix": [{"base": True, "write": BAD, "append": {"notes.txt": "x"}}, {"base": True, "write": BAD,
                                                                                     "append": {"notes.txt": "y"}}, "default"]})
    r = h.run("Build foo", "--model-build", "cheap", "--model-fix", "cheaper", "--max-fix-rounds", "3")
    assert r.rc == 0, r.err
    b, f1, f2, f3 = h.of("pbuild")[0], *h.of("fix")
    assert (b["model"], f1["model"], f2["model"], f3["model"]) == ("cheap", "cheaper", "cheaper", "strong")
    assert not f1["has_resume"] and "You take over an unattended build" in f1["prompt"] and "git diff --stat" in f1["prompt"]
    assert f2["resume"] == f1["session_id"]
    assert not f3["has_resume"] and "You take over" in f3["prompt"]


def test_fix_resumes_the_build_session_when_the_model_is_the_same(h: PH) -> None:
    h.scenario({"pbuild": [{"base": True, "write": BAD}], "fix": ["default"]})
    assert h.run().rc == 0
    assert h.of("fix")[0]["resume"] == h.of("pbuild")[0]["session_id"]
    assert "You take over" not in h.of("fix")[0]["prompt"]


def test_profile_model_build(tmp_path: Path) -> None:
    h = PH(tmp_path, profile(extra="test_gates: [unit]\nmodel_build: sonnet\n"))
    assert h.run().rc == 0
    assert h.of("ptests")[0]["model"] == "strong" and h.of("pbuild")[0]["model"] == "sonnet"


def test_fix_round_without_change_is_stuck_and_resumable(h: PH) -> None:
    h.scenario({"pbuild": [{"base": True, "write": BAD}],
                "fix": [{"base": True, "write": BAD}, {"base": True, "write": BAD}, "default"]})
    r = h.run()
    assert r.rc == 1 and FINAL(r.out) == "FORGE: failed"
    assert "stuck: fix round 1 changed nothing" in h.report()
    assert h.state()["stage"] == "plain-build"
    r2 = h.run()
    assert r2.rc == 0, r2.err
    assert h.stages() == ["ptests", "pbuild", "fix", "fix", "fix"]  # round 1, its escalation, the round after the rerun


def test_skip_base_check_still_runs_setup(tmp_path: Path) -> None:
    h = PH(tmp_path, PROFILE + "setup: python -c \"open('dep.txt', 'w').write('x')\"\n")
    assert h.run().rc == 0
    assert h.state()["base_checked"] == "skipped" and h.state()["setup_done"] is True


# ---- spec slices ------------------------------------------------------------------------------------
def big_spec(d: Path) -> Path:
    d.mkdir()
    pad = "x" * 20000
    (d / "spec.yaml").write_text(
        "functional_requirements:\n  - id: FR-1\n    text: one\n  - id: FR-2\n    text: two\n"
        f"user_stories:\n  - id: US1\n    title: story\n    note: {pad}\n", encoding="utf-8")
    (d / "tasks.yaml").write_text(
        "tasks:\n  - id: T001\n    title: first\n    requirement_refs: [FR-1]\n    contract_refs: [C-1]\n"
        "    entity_refs: [E-2]\n    acceptance_criteria:\n      - id: AC-1\n        description: works\n"
        "  - id: T002\n    title: second\n    requirement_refs: [FR-2]\n", encoding="utf-8")
    (d / "contracts.yaml").write_text("contracts:\n  - id: C-1\n    name: Poll\n  - id: C-2\n    name: Other\n",
                                      encoding="utf-8")
    (d / "data-model.yaml").write_text("entities:\n  - id: E-1\n    name: A\n  - id: E-2\n    name: B\n",
                                       encoding="utf-8")
    return d


def test_slice_takes_only_the_units_items(tmp_path: Path) -> None:
    d = big_spec(tmp_path / "spec")
    text = spec_slice.build(d, {"requirements": ["FR-1"], "spec": "do it"}, tmp_path)
    assert "id: FR-1" in text and "FR-2" not in text and "T001" in text and "T002" not in text
    assert "Poll" in text and "Other" not in text and "name: B" in text and "name: A" not in text
    assert "`spec/spec.yaml`" in text and len(text) < 3000
    assert "id: AC-1" not in text and "AC-1: description=works" in text


def test_slice_ids_named_in_the_unit_text(tmp_path: Path) -> None:
    d = big_spec(tmp_path / "spec")
    text = spec_slice.build(d, {"requirements": [], "spec": "implement T002 and contract C-2"}, tmp_path)
    assert "second" in text and "Other" in text and "first" not in text


def test_small_spec_is_inlined_whole(tmp_path: Path) -> None:
    d = tmp_path / "spec"
    d.mkdir()
    (d / "spec.md").write_text("R1: tiny spec\n", encoding="utf-8")
    assert "R1: tiny spec" in spec_slice.build(d, {"requirements": []}, tmp_path)


def test_markdown_spec_keeps_paragraphs_with_the_ids(tmp_path: Path) -> None:
    d = tmp_path / "spec"
    d.mkdir()
    (d / "spec.md").write_text("FR-1: keep me\n\n" + ("noise " * 4000) + "\n\nFR-2: drop me\n", encoding="utf-8")
    text = spec_slice.build(d, {"requirements": ["FR-1"]}, tmp_path)
    assert "keep me" in text and "drop me" not in text and "noise" not in text


# ---- queue_run --------------------------------------------------------------------------------------
def test_rate_slots(tmp_path: Path) -> None:
    f = tmp_path / "rate.json"
    now = time.time()
    assert queue_run.rate_slots(f, 4, now) == (4, 0.0)
    for status, util, want in (("allowed", 0.5, 4), ("allowed", 0.8, 2), ("allowed", 0.95, 1), ("allowed_warning", 0.5, 1)):
        f.write_text(json.dumps({"status": status, "utilization": util, "at": now}), encoding="utf-8")
        assert queue_run.rate_slots(f, 4, now)[0] == want
    f.write_text(json.dumps({"status": "rejected", "resets_at": now + 600, "at": now}), encoding="utf-8")
    assert queue_run.rate_slots(f, 4, now) == (0, now + 660)
    f.write_text(json.dumps({"status": "rejected", "resets_at": now + 600, "at": now - 3600}), encoding="utf-8")
    assert queue_run.rate_slots(f, 4, now) == (4, 0.0)


def test_already_merged_units_unblock_their_dependents(tmp_path: Path) -> None:
    qh = QH(tmp_path)
    r = qh.run(qh.queue([unit("U1"), unit("U2", deps=("U1",))], already_merged=["U1"]))
    assert r.rc == 0, r.out
    assert qh.statuses() == {"U1": "merged", "U2": "merged"} and qh.stages("U1") == []


def test_failed_unit_gets_a_second_attempt_with_the_reason(tmp_path: Path) -> None:
    qh = QH(tmp_path)
    r = qh.run(qh.queue([unit("U1", fake=("fail_once",))], max_attempts=2))
    assert r.rc == 0, r.out
    v = qh.state()["units"]["U1"]
    assert v["status"] == "merged" and v["attempts"] == 1 and v["history"][0]["reason"]
    assert (qh.run_dir / "forge" / "U1.attempt1").is_dir()
    assert "attempt 2/2" in r.out
    s = qh.summary()  # the failed first attempt was paid for: it is in the unit's and the run's cost
    one = v["spent_before"]
    assert one > 0 and s["cost"]["total_usd"] == pytest.approx(one + v["cost"]["total_usd"])
    assert sum(s["cost"]["by_model"].values()) == pytest.approx(s["cost"]["total_usd"])
    row = next(u for u in s["units"] if u["id"] == "U1")
    assert row["cost_all_attempts"] == pytest.approx(s["cost"]["total_usd"])


def test_attempts_exhausted_needs_a_human(tmp_path: Path) -> None:
    qh = QH(tmp_path)
    r = qh.run(qh.queue([unit("U1", fake=("fail",))], max_attempts=2))
    assert r.rc == 1, r.out
    v = qh.state()["units"]["U1"]
    assert v["status"] == "failed" and "after 2 attempts; needs a human" in v["reason"]


def test_parallel_units_merge_into_one_branch(tmp_path: Path) -> None:
    qh = QH(tmp_path)
    units = [unit("U1"), unit("U2"), unit("U3", deps=("U1", "U2"))]
    r = qh.run(qh.queue(units), "--parallel", "2")
    assert r.rc == 0, r.out
    assert qh.statuses() == {"U1": "merged", "U2": "merged", "U3": "merged"}
    assert {"out_U1.txt", "out_U2.txt", "out_U3.txt"} <= qh.tree("powerpuff/run-r1")


def test_queue_passes_build_and_fix_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_plain_engine import queue_cmd
    cmd = queue_cmd(tmp_path, monkeypatch, (), model_build="sonnet", model_fix="haiku")
    assert cmd[cmd.index("--model-build") + 1] == "sonnet" and cmd[cmd.index("--model-fix") + 1] == "haiku"
    assert "--model-build" not in queue_cmd(tmp_path / "b", monkeypatch) if (tmp_path / "b").mkdir() is None else True


def test_unit_usd_caps_every_unit(tmp_path: Path) -> None:
    cfg = {"max_usd": 1000.0, "fix_reserve": 0.33, "unit_usd": 25}
    assert queue_run.caps(cfg, 64)[1] == 25
