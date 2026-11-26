"""Gate profile, anti-tampering and missing-tool behaviour of forge_run / forge_check (fake claude, never the real one)."""

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))
import forge_check  # noqa: E402
import gate_guard  # noqa: E402
from fake_claude import GATE, GOOD_APP  # noqa: E402
from test_forge_run import BAD, FINAL, Harness  # noqa: E402

NOT_APPROVED = ("- gates: NOT APPROVED (chosen by the model; review .forge/powerpuff.draft.yaml and save it as "
                "powerpuff.yaml)")


def profile(approved: str = "approved: true\n", gates: str = "  iteration: [unit]\n", extra: str = "",
            commands: dict | None = None) -> str:
    cmds = commands if commands is not None else {"unit": GATE}
    lines = "".join(f"  {k}: '{v}'\n" for k, v in cmds.items())
    return f"{approved}gates:\n{gates}commands:\n{lines}{extra}"


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def put_profile(h: Harness, text: str) -> None:
    (h.repo / "powerpuff.yaml").write_text(text, encoding="utf-8")


def fix_check(h: Harness) -> dict:
    fix = next(c for c in h.calls() if c["stage"] == "fix")
    return json.loads(fix["prompt"].split("forge_check output:\n", 1)[1])


def test_profile_used_and_auditor_not_asked_for_gates(h: Harness) -> None:
    put_profile(h, profile(gates="  iteration: [unit]\n  unit: [unit]\n  final: [mutation-diff]\n",
                           commands={"unit": GATE, "mutation-diff": 'python -c "open(chr(102), chr(119))"'}))
    h.scenario({"audit": [{"write": {".forge/requirements.md": "R1: a\nR2: b\n", ".forge/digest.md": "d\n"}}]})
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    audit = h.calls()[0]
    assert "Do NOT write `.forge/gates.json`" in audit["prompt"] and "exactly two files" in audit["prompt"]
    assert not (h.repo / ".forge" / "gates.json").exists()
    st = h.state()
    assert st["gates_source"] == "profile" and st["gates"] == {"unit": GATE}
    assert st["final_gates"] == ["mutation-diff"]
    assert not (h.repo / "f").exists()
    assert "- gates: approved profile" in h.report()
    assert "- guard: ok=True" in h.report()
    assert GATE in next(c for c in h.calls() if c["stage"] == "implement")["prompt"]


def test_inner_loop_replaces_gates_in_the_session_prompt(h: Harness) -> None:
    put_profile(h, profile(extra="inner_loop: 'python -c \"pass\"'\n"))
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    impl = next(c for c in h.calls() if c["stage"] == "implement")["prompt"]
    assert "inner loop (YOU run this one, it is fast): `python -c \"pass\"`" in impl
    assert "do not run them yourself" in impl and GATE in impl


def test_no_inner_loop_keeps_plain_gate_list(h: Harness) -> None:
    put_profile(h, profile())
    assert h.run("Build foo").rc == 0
    assert "inner loop" not in next(c for c in h.calls() if c["stage"] == "implement")["prompt"]


def test_missing_command_fails_before_any_session(h: Harness) -> None:
    put_profile(h, profile(gates="  iteration: [unit, lint]\n"))
    r = h.run("Build foo")
    assert r.rc == 1 and FINAL(r.out) == "FORGE: failed"
    assert h.calls() == []
    assert "gate 'lint' is listed in gates.iteration but has no command" in h.report()


def test_no_profile_writes_draft_and_not_approved_line(h: Harness) -> None:
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    st = h.state()
    assert st["gates_source"] == "model-draft"
    assert NOT_APPROVED in h.report()
    draft = (h.repo / ".forge" / "powerpuff.draft.yaml").read_text(encoding="utf-8")
    assert draft.startswith("approved: false\n") and "iteration: ['unit']" in draft
    parsed = gate_guard.parse_simple_yaml(draft)
    assert parsed["gates"] == {"iteration": ["unit"]} and parsed["commands"] == {"unit": GATE}


def test_unapproved_profile_line(h: Harness) -> None:
    put_profile(h, profile(approved=""))
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    assert "- gates: profile not approved" in h.report()
    assert not (h.repo / ".forge" / "powerpuff.draft.yaml").exists()


def test_approved_profile_line(h: Harness) -> None:
    put_profile(h, profile())
    assert h.run("Build foo").rc == 0
    assert "- gates: approved profile" in h.report()


def test_missing_tool_gate_fails_with_flag(tmp_path: Path) -> None:
    out = forge_check.check_gates(tmp_path, 30, {"x": "definitely_not_a_tool_xyz --version"})
    assert out["ok"] is False
    assert out["results"]["x"]["ok"] is False and out["results"]["x"]["missing_tool"] is True


def test_nonzero_exit_is_not_missing_tool(tmp_path: Path) -> None:
    out = forge_check.check_gates(tmp_path, 30, {"x": 'python -c "raise SystemExit(3)"'})
    assert out["results"]["x"]["ok"] is False and "missing_tool" not in out["results"]["x"]


@pytest.mark.parametrize("code,text,expected", [(127, "", True), (9009, "", True), (1, "'x' is not recognized as an "
                                                "internal or external command", True), (1, "FAILED", False),
                                                (0, "", False)])
def test_tool_missing_detection(code: int, text: str, expected: bool) -> None:
    assert forge_check.tool_missing(code, text) is expected


def test_empty_gate_set_fails(tmp_path: Path) -> None:
    assert forge_check.check_gates(tmp_path, 30, {})["ok"] is False


@pytest.mark.parametrize("impl,fix", [
    ({"base": True, "append": {"app.py": "\n# noqa\n"}}, {"base": True}),
    ({"base": True, "write": {"tests/test_unit.py": "import pytest\n\n\n@pytest.mark.skip\ndef test_x():\n    pass\n"}},
     {"base": True, "delete": ["tests/test_unit.py"]}),
])
def test_guard_violation_reaches_next_round_and_is_removed(h: Harness, impl: dict, fix: dict) -> None:
    h.scenario({"implement": [impl], "fix": [fix]})
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    assert h.stages() == ["audit", "tests", "implement", "fix", "accept"]
    rep = fix_check(h)
    assert rep["done"] is False and rep["gates"]["ok"] is True
    assert rep["guard"]["ok"] is False and rep["guard"]["counts"]
    assert {v["rule"] for v in rep["guard"]["violations"]} <= {"suppression-added", "skip-added"}
    assert "must be removed, not worked around" in next(c for c in h.calls() if c["stage"] == "fix")["prompt"]
    assert "- guard: ok=True" in h.report()


def test_unknown_extension_reported_blind(h: Harness) -> None:
    h.scenario({"implement": [{"base": True, "write": {"tool.rb": "puts 1\n"}}]})
    assert h.run("Build foo").rc == 0
    assert "- guard is blind for: .rb" in h.report()


def test_session_rewriting_gates_json_is_detected_and_not_honoured(h: Harness) -> None:
    trivial = json.dumps({"format_fix": None, "gates": {"unit": "python -c pass"}})
    h.scenario({"implement": [{"write": {**BAD, ".forge/gates.json": trivial}}]})
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    rep = fix_check(h)
    assert rep["gates_tampered"] is True and rep["done"] is False
    assert rep["gates"]["results"]["unit"]["ok"] is False
    on_disk = json.loads((h.repo / ".forge" / "gates.json").read_text(encoding="utf-8"))
    assert on_disk["gates"] == {"unit": GATE}
    assert h.state()["gates"] == {"unit": GATE}
    assert "gates_tampered" in h.report()


def test_state_json_gates_edited_between_runs_fail_on_resume(h: Harness) -> None:
    sp = h.repo / ".forge" / "state.json"
    h.run("Build foo", "--preflight")
    st = json.loads(sp.read_text(encoding="utf-8"))
    st["gates"] = {"unit": "python -c pass"}
    sp.write_text(json.dumps(st), encoding="utf-8")
    r = h.run("Build foo")
    assert r.rc == 1 and "hash mismatch" in h.report()
    assert h.stages() == ["audit"]


def test_session_editing_profile_is_restored_and_flagged(h: Harness) -> None:
    put_profile(h, profile())
    weak = profile(commands={"unit": "python -c pass"})
    h.scenario({"implement": [{"write": {**BAD, "powerpuff.yaml": weak}}]})
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    rep = fix_check(h)
    assert rep["gates_tampered"] is True and rep["gates"]["results"]["unit"]["ok"] is False
    assert (h.repo / "powerpuff.yaml").read_text(encoding="utf-8") == profile()


def test_gates_json_hash_mismatch_on_resume_fails(h: Harness) -> None:
    h.run("Build foo", "--preflight")
    (h.repo / ".forge" / "gates.json").write_text(
        json.dumps({"format_fix": None, "gates": {"unit": "python -c pass"}}), encoding="utf-8")
    r = h.run("Build foo")
    assert r.rc == 1 and FINAL(r.out) == "FORGE: failed"
    assert "hash mismatch" in h.report()
    assert h.stages() == ["audit"]


def test_resume_with_profile_reloads_gates(h: Harness) -> None:
    put_profile(h, profile())
    h.scenario({"implement": [{"kill_driver": True}, "default"]})
    h.run("Build foo")
    st = json.loads((h.repo / ".forge" / "state.json").read_text(encoding="utf-8"))
    st["gates"] = {"unit": "python -c pass"}
    (h.repo / ".forge" / "state.json").write_text(json.dumps(st), encoding="utf-8")
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    assert h.stages().count("audit") == 1 and h.stages().count("implement") == 2
    st = h.state()
    assert st["gates_source"] == "profile" and st["profile_approved"] is True
    assert "- gates: approved profile" in h.report()


def test_frozen_sha_moves_forward_for_guard(h: Harness) -> None:
    h.scenario({"accept": [{"write": {"tests/test_acceptance.py": "# R1\n# R2\n",
                                      ".forge/unfrozen.md": "- tests/test_acceptance.py: wrong\n",
                                      ".forge/gaps.json": json.dumps({"gaps": [], "questions": []})}}]})
    r = h.run("Build foo")
    assert r.rc == 0, r.err
    assert "- guard: ok=True" in h.report()
    assert GOOD_APP in (h.repo / "app.py").read_text(encoding="utf-8")


def test_failure_brief_keeps_only_red_parts():
    import forge_run
    rep = {"done": False, "coverage": {"ok": True}, "frozen": {"ok": True},
           "guard": {"ok": False, "violations": [{"rule": "skip-added", "file": "t.py", "line": 1, "text": "x" * 50}] * 20},
           "gates": {"ok": False, "results": {"lint": {"ok": True, "tail": ""}, "test": {"ok": False, "tail": "boom"}}}}
    brief = json.loads(forge_run.failure_brief(rep))
    assert brief["done"] is False and brief["coverage"] == {"ok": True}
    assert list(brief["gates"]["results"]) == ["test"]
    assert len(brief["guard"]["violations"]) == 20
