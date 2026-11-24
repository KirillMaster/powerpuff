"""Red-on-base check of forge_run: acceptance tests must fail before any implementation exists (fake claude only)."""

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))
from fake_claude import GATE  # noqa: E402
from test_forge_run import FINAL, Harness  # noqa: E402

LINE = "- acceptance tests fail before implementation: "
VACUOUS_LINE = "- acceptance tests: PASS WITHOUT IMPLEMENTATION (vacuous) - review"
GREEN_UNTIL_NEW_TEST = ('python -c "import os, sys; sys.exit(1 if os.path.exists(\'tests/test_acceptance_vacuous.py\') '
                        'and not os.path.exists(\'impl.flag\') else 0)"')
COUNTING_GATE = 'python -c "import app; open(\'.forge/runs.txt\', \'a\').write(\'x\'); assert app.foo() == 42"'


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def audit(gates: dict) -> dict:
    return {"audit": [{"base": True, "write": {".forge/gates.json": json.dumps({"format_fix": None, "gates": gates})}}]}


def runs(h: Harness) -> int:
    return len((h.repo / ".forge" / "runs.txt").read_text(encoding="utf-8"))


def test_red_on_base_no_extra_session(h: Harness) -> None:
    h.scenario(audit({"test": GATE}))
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    assert h.stages() == ["audit", "tests", "implement", "accept"]
    assert h.state()["red_on_base"] is True
    rep = h.report()
    assert LINE + "yes" in rep and VACUOUS_LINE not in rep


def test_vacuous_tests_get_one_writer_session_then_red(h: Harness) -> None:
    h.scenario({**audit({"test": GREEN_UNTIL_NEW_TEST}),
                "implement": [{"base": True, "write": {"impl.flag": "1"}}]})
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    assert FINAL(r.out) == "FORGE: done"
    assert h.stages() == ["audit", "tests", "vacuous", "implement", "accept"]
    vac = h.calls()[2]
    assert vac["model"] == "writer" and not vac["has_resume"]
    assert h.state()["red_on_base"] is True
    assert "tests/test_acceptance_vacuous.py" in (h.repo / ".forge" / "frozen.sha256").read_text(encoding="utf-8")
    rep = h.report()
    assert LINE + "yes" in rep and VACUOUS_LINE not in rep


def test_still_vacuous_does_not_fail_the_run(h: Harness) -> None:
    h.scenario({**audit({"test": "python -c pass"}), "vacuous": [{}]})
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    assert FINAL(r.out) == "FORGE: done"
    assert h.stages().count("vacuous") == 1
    assert h.state()["red_on_base"] is False
    rep = h.report()
    assert LINE + "no (vacuous)" in rep and VACUOUS_LINE in rep


def test_profile_test_gates_key_selects_gates(h: Harness) -> None:
    (h.repo / "powerpuff.yaml").write_text(
        f"approved: true\ngates:\n  iteration: [unit, lint]\ncommands:\n  unit: '{GATE}'\n  lint: 'python -c pass'\n"
        "test_gates: [unit]\n", encoding="utf-8")
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    assert h.stages() == ["audit", "tests", "implement", "accept"]
    assert h.state()["red_on_base"] is True
    assert LINE + "yes" in h.report()


def test_profile_test_gates_unknown_id_is_configuration_error(h: Harness) -> None:
    (h.repo / "powerpuff.yaml").write_text(
        f"approved: true\ngates:\n  iteration: [unit]\ncommands:\n  unit: '{GATE}'\ntest_gates: [nope]\n",
        encoding="utf-8")
    r = h.run("Build foo")
    assert r.rc == 1 and h.calls() == []
    assert "test_gates" in h.report()


def test_profile_gate_named_test_is_picked_without_test_gates_key(h: Harness) -> None:
    (h.repo / "powerpuff.yaml").write_text(
        f"approved: true\ngates:\n  iteration: [lint]\n  unit: [acceptance]\ncommands:\n  lint: 'python -c pass'\n"
        f"  acceptance: '{GATE}'\n", encoding="utf-8")
    assert h.run("Build foo").rc == 0
    assert h.state()["red_on_base"] is True


def test_no_test_gate_recorded_without_session(h: Harness) -> None:
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    assert h.stages() == ["audit", "tests", "implement", "accept"]
    assert h.state()["red_on_base"] == "no test gate"
    assert LINE + "no test gate" in h.report()


def test_missing_tool_for_test_gate_fails_before_build(h: Harness) -> None:
    h.scenario(audit({"test": "definitely_not_a_tool_xyz --version"}))
    r = h.run("Build foo")
    assert r.rc == 1 and FINAL(r.out) == "FORGE: failed"
    assert "implement" not in h.stages() and h.stages() == ["audit", "tests"]
    assert "test gate 'test' cannot run" in h.report()


def test_resume_after_the_check_does_not_repeat_it(h: Harness) -> None:
    h.scenario({**audit({"test": COUNTING_GATE}), "implement": [{"kill_driver": True}, "default"]})
    h.run("Build foo")
    assert h.state()["red_on_base"] is True and runs(h) == 1
    st = h.state()
    st["stage"] = "tests"
    (h.repo / ".forge" / "state.json").write_text(json.dumps(st), encoding="utf-8")
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    assert runs(h) == 2  # one check after the build round (the end reuses it), no second red-on-base run
    assert "vacuous" not in h.stages()


def test_older_state_without_the_key_is_unknown_and_runs_no_check(h: Harness) -> None:
    h.scenario({**audit({"test": COUNTING_GATE}), "implement": [{"kill_driver": True}, "default"]})
    h.run("Build foo")
    st = h.state()
    st.pop("red_on_base")
    (h.repo / ".forge" / "state.json").write_text(json.dumps(st), encoding="utf-8")
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    assert runs(h) == 2  # the build check; the end reuses it on the unchanged tree
    assert "red_on_base" not in h.state()
    assert LINE + "unknown" in h.report()
