"""Tests for scripts/prepare_run.py driven end to end against a scripted fake `claude` on PATH (never the real CLI)."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DRIVER = ROOT / "scripts" / "prepare_run.py"
sys.path.insert(0, str(ROOT / "scripts"))
import queue_run  # noqa: E402

FAKE_SRC = '''
import json, os, signal, sys, time
from pathlib import Path

HEADS = [("You are the spec auditor of a prepared run", "questions"),
         ("You slice a spec into work units", "slice"), ("The unit draft failed validation", "repair")]
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
argv = sys.argv[1:]
model = argv[argv.index("--model") + 1] if "--model" in argv else None
tools = argv[argv.index("--allowedTools") + 1] if "--allowedTools" in argv else None
prompt = sys.stdin.read()
first = prompt.lstrip().splitlines()[0] if prompt.strip() else ""
stage = next((n for h, n in HEADS if first.startswith(h)), "unknown")
scn_path = Path(os.environ["FAKE_CLAUDE_SCENARIO"])
scn = json.loads(scn_path.read_text(encoding="utf-8"))
cp = Path(str(scn_path) + ".counters.json")
counters = json.loads(cp.read_text(encoding="utf-8")) if cp.is_file() else {}
idx = counters.get(stage, 0)
counters[stage] = idx + 1
cp.write_text(json.dumps(counters), encoding="utf-8")
entries = scn.get(stage) or [{}]
entry = entries[min(idx, len(entries) - 1)]
with open(str(scn_path) + ".calls.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps({"stage": stage, "index": idx, "model": model, "tools": tools, "prompt": prompt}) + "\\n")
if entry.get("kill_driver"):
    os.kill(int(Path(os.environ["FAKE_CLAUDE_PIDFILE"]).read_text().strip()), signal.SIGTERM)
    time.sleep(0.5)
    sys.exit(1)
kind = entry.get("result", "ok")
cost = entry.get("cost", 0.25)
text = entry.get("text", "")
if kind == "rate_limit":
    text = "Claude AI usage limit reached|" + str(int(time.time()) + 3600)
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s"}), flush=True)
print(json.dumps({"type": "result", "subtype": "error" if kind != "ok" else "success", "is_error": kind != "ok",
                  "result": text, "total_cost_usd": cost, "num_turns": 2, "session_id": "s",
                  "modelUsage": {model: {"costUSD": cost}}}), flush=True)
'''

SPEC_MD = """# Feature Specification: Pocket ledger notes

**Branch**: `main` | **Created**: 2026-01-10 | **Status**: Approved

## User Story 1 - Save a note (Priority: P1)

The user saves a note from the command line.

- `AS-1` Given an empty ledger, when the user saves a note, then the ledger holds one note
- `AS-2` Given a ledger holding a note, when the user saves it again, then the ledger still holds one note
- `AC-1` Saving the same note twice is idempotent and never duplicates data

- `TS-1` (unit)
  - Covers: AS-1
  - Then: the ledger file holds one note
- `TS-2` (unit)
  - Covers: AS-2
  - Then: the ledger file still holds one note

## Requirements

- **FR-001**: The tool MUST store each note with a title and a timestamp (US1).
- **FR-002**: The tool MUST ignore a note identical to an existing one (US1).
"""
TASKS_MD = """# Tasks: Pocket ledger notes

## `T001` Make saving idempotent [US1]

Modify src/store.py so that saving an identical note twice keeps a single entry.

- **Depends on**: -
- **Requirements**: FR-001, FR-002
"""

QTEXT = ("Q1 [blocking]: Which file format does the ledger use? | default: json | cost of a wrong guess: data migration\n"
         "Q2 [non-blocking]: Trim titles? | default: yes | cost of a wrong guess: cosmetic\n"
         "D1: ordering -> insertion order - existing code\nQUESTIONS: done")


def units_text(*units: dict) -> str:
    return "Here is the draft.\n```yaml\n" + json.dumps(list(units)) + "\n```\n"


def unit(uid: str, deps: list[str], reqs: list[str], files: list[str], risk: str = "low", est: int = 80) -> dict:
    return {"id": uid, "title": f"Unit {uid}", "deps": deps, "risk": risk, "requirements": reqs, "files": files,
            "est_lines": est, "spec": f"Build {uid}.\nAssert the behaviour of {', '.join(reqs)}.\n"}


U1 = unit("U1", [], ["FR-001", "AS-1"], ["src/store.py"])
U2 = unit("U2", ["U1"], ["FR-002", "AS-2"], ["src/store.py", "src/cli.py"], risk="high", est=120)
GOOD = units_text(U1, U2)


class Res:
    def __init__(self, rc: int, out: str, err: str):
        self.rc, self.out, self.err = rc, out, err

    @property
    def last(self) -> str:
        return self.out.strip().splitlines()[-1] if self.out.strip() else ""


class Harness:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.bindir = tmp / "bin"
        self.bindir.mkdir()
        fake = tmp / "fake_prepare_claude.py"
        fake.write_text(FAKE_SRC, encoding="utf-8")
        (self.bindir / "claude.cmd").write_text(f'@"{sys.executable}" "{fake}" %*\r\n', encoding="ascii")
        sh = self.bindir / "claude"
        sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n', encoding="ascii")
        sh.chmod(sh.stat().st_mode | stat.S_IEXEC)
        self.scn, self.pidfile, self.out = tmp / "scenario.json", tmp / "driver.pid", tmp / "queue.yaml"
        self.repo = tmp / "repo"
        for rel, text in (("specs/f1/spec.md", SPEC_MD), ("specs/f1/tasks.md", TASKS_MD), ("src/store.py", "x = 1\n"),
                          ("src/cli.py", "y = 1\n"), (".gitignore", "__pycache__/\n")):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text, encoding="utf-8")
        self.spec = str(self.repo / "specs" / "f1")
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--no-gpg-sign", "-m", "init"]):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)
        self.n = 0
        self.scenario()

    def scenario(self, **stages: list[dict]) -> None:
        base = {"questions": [{"text": QTEXT}], "slice": [{"text": GOOD}], "repair": [{"text": GOOD}]}
        self.scn.write_text(json.dumps({**base, **stages}), encoding="utf-8")

    def run(self, spec: str | None = None, *args: str, timeout: int = 120) -> Res:
        self.n += 1
        env = {**os.environ, "PATH": str(self.bindir) + os.pathsep + os.environ["PATH"],
               "FAKE_CLAUDE_SCENARIO": str(self.scn), "FAKE_CLAUDE_PIDFILE": str(self.pidfile),
               "PYTHONUNBUFFERED": "1"}
        cmd = [sys.executable, str(DRIVER), spec or self.spec, "--repo", str(self.repo), "--model", "strong",
               "--out", str(self.out), *args]
        out_p, err_p = self.tmp / f"out{self.n}.txt", self.tmp / f"err{self.n}.txt"
        with open(out_p, "w", encoding="utf-8") as so, open(err_p, "w", encoding="utf-8") as se:
            p = subprocess.Popen(cmd, cwd=self.tmp, env=env, stdout=so, stderr=se)
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

    def prompt(self, stage: str, n: int = -1) -> str:
        return [c for c in self.calls() if c["stage"] == stage][n]["prompt"]

    @property
    def state_dir(self) -> Path:
        return next((self.repo / ".powerpuff" / "prepare").iterdir())

    def state(self) -> dict:
        return json.loads((self.state_dir / "state.json").read_text(encoding="utf-8"))

    def answer_blocking(self, text: str) -> None:
        p = self.state_dir / "questions.json"
        qs = json.loads(p.read_text(encoding="utf-8"))
        for q in qs:
            if q["class"] == "blocking":
                q["answer"] = text
        p.write_text(json.dumps(qs), encoding="utf-8")

    def queue_errors(self) -> list[str]:
        return queue_run.normalize(queue_run.load_queue(self.out), self.out, self.repo)[1]


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_happy_path_with_assumed_defaults(h: Harness) -> None:
    r = h.run(None, "--assume-defaults")
    assert r.rc == 0, r.out + r.err
    assert r.last == f"PREPARE: ready {h.out.resolve()}"
    assert "gates: no approved profile" in r.out
    assert "QUEUE: dry-run ok" in r.out and "1. U1" in r.out and "2. U2" in r.out
    assert h.stages() == ["questions", "slice"]
    assert {c["model"] for c in h.calls()} == {"strong"} and {c["tools"] for c in h.calls()} == {"Read,Grep,Glob"}
    assert h.queue_errors() == []
    head = h.out.read_text(encoding="utf-8").splitlines()[:6]
    assert "# units: 2 (low 1, high 1)" in head
    assert "# requirements covered: 4/4" in head
    assert "# questions: answered 0, defaulted 2" in head
    assert any(x.startswith("# start: python ") and "queue_run.py" in x for x in head)
    cfg = queue_run.normalize(queue_run.load_queue(h.out), h.out, h.repo)[0]
    assert cfg["auto_merge"] == ["low"] and cfg["base"] == subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=h.repo, capture_output=True, text=True).stdout.strip()
    assert [u["risk"] for u in cfg["units"]] == ["low", "high"] and cfg["units"][1]["deps"] == ["U1"]
    st = h.state()
    assert st["cost"]["total_usd"] == pytest.approx(0.5) and st["cost"]["sessions"] == 2
    assert ".powerpuff/" in (h.repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "[blocking, assumed]" in h.prompt("slice")
    assert "D1: ordering" in h.prompt("slice")


def test_lint_failure_stops_before_any_session(h: Harness) -> None:
    spec = h.repo / "specs" / "f1" / "spec.md"
    spec.write_text(SPEC_MD.replace("existing one (US1)", "existing one [NEEDS CLARIFICATION: when?] (US1)"),
                    encoding="utf-8")
    r = h.run()
    assert r.rc == 1 and "unresolved-clarification" in r.out and r.last.startswith("PREPARE: failed (spec lint")
    assert h.calls() == [] and not h.out.exists()
    lint = json.loads((h.state_dir / "lint.json").read_text(encoding="utf-8"))
    assert not lint["ok"] and lint["errors"]
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "unclear", "--no-gpg-sign"],
                   cwd=h.repo, check=True, capture_output=True)
    r = h.run(None, "--accept-lint", "--assume-defaults")
    assert r.rc == 0, r.out + r.err
    assert "# lint accepted: 1 error(s)" in h.out.read_text(encoding="utf-8")
    assert h.stages() == ["questions", "slice"]


def test_clean_lint_is_not_an_obstacle(h: Harness) -> None:
    r = h.run(None, "--assume-defaults")
    assert r.rc == 0 and h.state()["lint"] == "ok"


def test_red_base_stops_before_any_session(h: Harness) -> None:
    (h.repo / "powerpuff.yaml").write_text(
        "approved: true\ngates:\n  unit: [tests, lint]\ncommands:\n  tests: 'python -c \"raise SystemExit(1)\"'\n"
        "  lint: 'python -c \"pass\"'\n", encoding="utf-8")
    r = h.run()
    assert r.rc == 1 and "base is red: tests" in r.last and "lint" not in r.last
    assert h.calls() == [] and not h.out.exists()


def test_green_base_with_profile_runs_and_final_commands_reach_the_queue(h: Harness) -> None:
    (h.repo / "powerpuff.yaml").write_text(
        "approved: true\ngates:\n  unit: [lint]\n  final: [smoke]\ncommands:\n  lint: 'python -c \"pass\"'\n"
        "  smoke: 'python -c \"print(1)\"'\n", encoding="utf-8")
    r = h.run(None, "--assume-defaults")
    assert r.rc == 0, r.out + r.err
    assert "gates: no approved profile" not in r.out
    cfg = queue_run.normalize(queue_run.load_queue(h.out), h.out, h.repo)[0]
    assert cfg["final"]["commands"] == {"smoke": 'python -c "print(1)"'}


def test_blocking_question_exit_4_then_answer_without_repeating_the_audit(h: Harness) -> None:
    r = h.run()
    assert r.rc == 4 and h.stages() == ["questions"]
    assert "| 1 | Q1 | Which file format does the ledger use? | json | data migration |" in r.out
    assert "Q2" not in r.out
    qpath = h.state_dir / "questions.json"
    assert r.last == f"PREPARE: needs answers {qpath.relative_to(h.repo).as_posix()}"
    qs = json.loads(qpath.read_text(encoding="utf-8"))
    assert [(q["id"], q["class"], q["answer"]) for q in qs] == [("Q1", "blocking", None), ("Q2", "non-blocking", None)]
    assert not h.out.exists()
    h.answer_blocking("sqlite, one table per ledger")
    r = h.run()
    assert r.rc == 0, r.out + r.err
    assert h.stages() == ["questions", "slice"]
    assert "Human decisions (binding):" in h.prompt("slice") and "sqlite, one table per ledger" in h.prompt("slice")
    head = h.out.read_text(encoding="utf-8")
    assert "# questions: answered 1, defaulted 1" in head


def test_answers_file(h: Harness) -> None:
    assert h.run().rc == 4
    answers = h.tmp / "answers.json"
    answers.write_text(json.dumps([{"id": "Q1", "answer": "postgres"}]), encoding="utf-8")
    r = h.run(None, "--answers", str(answers))
    assert r.rc == 0, r.out + r.err
    assert "postgres" in h.prompt("slice") and h.stages() == ["questions", "slice"]
    assert json.loads((h.state_dir / "questions.json").read_text(encoding="utf-8"))[0]["answer"] == "postgres"


BAD_DRAFTS = {
    "cycle": (units_text(unit("U1", ["U2"], ["FR-001", "AS-1"], ["src/store.py"]),
                         unit("U2", ["U1"], ["FR-002", "AS-2"], ["src/cli.py"])),
              ["dependency cycle through U1, U2"]),
    "overlap": (units_text(unit("U1", [], ["FR-001", "AS-1"], ["src/store.py"]),
                           unit("U2", [], ["FR-002", "AS-2"], ["src/store.py"])),
                ["U1 and U2 share src/store.py but neither depends on the other"]),
    "uncovered": (units_text(unit("U1", [], ["FR-001", "AS-1", "FR-002"], ["src/store.py"])),
                  ["requirement AS-2 is not assigned to any unit"]),
    "too_big": (units_text(unit("U1", [], ["FR-001", "AS-1"], ["src/store.py"], est=450), U2),
                ["U1: est_lines 450 exceeds --max-unit-lines 200 (split the unit)"]),
    "unknown_dep": (units_text(U1, unit("U2", ["U9"], ["FR-002", "AS-2"], ["src/cli.py"])),
                    ["U2: unknown dep U9"]),
    "missing_dir": (units_text(U1, unit("U2", ["U1"], ["FR-002", "AS-2"], ["nodir/x.py"])),
                    ["U2: directory nodir/ of nodir/x.py does not exist and no earlier unit in its dependency chain "
                     "creates it"]),
    "no_fence": ("I could not decide.", ["the answer is not a list of units"]),
}


@pytest.mark.parametrize("name", sorted(BAD_DRAFTS))
def test_invalid_draft_goes_through_one_repair_round(h: Harness, name: str) -> None:
    bad, problems = BAD_DRAFTS[name]
    h.scenario(slice=[{"text": bad}])
    r = h.run(None, "--assume-defaults")
    assert r.rc == 0, r.out + r.err
    assert h.stages() == ["questions", "slice", "repair"]
    repair = h.prompt("repair")
    for p in problems:
        assert f"- {p}" in repair
    assert h.queue_errors() == []
    assert "Human decisions" in repair or "Defaults taken" in repair


def test_exact_problem_list_in_the_repair_prompt(h: Harness) -> None:
    bad = units_text(unit("U1", [], ["FR-001"], ["src/store.py"], est=300), unit("U2", ["U7"], ["FR-001"], ["src/cli.py"]))
    h.scenario(slice=[{"text": bad}])
    assert h.run(None, "--assume-defaults").rc == 0
    block = h.prompt("repair").split("Problems:\n", 1)[1].split("\n\n", 1)[0]
    assert block.splitlines() == [
        "- U2: unknown dep U7",
        "- U1: est_lines 300 exceeds --max-unit-lines 200 (split the unit)",
        "- requirement AS-1 is not assigned to any unit",
        "- requirement AS-2 is not assigned to any unit",
        "- requirement FR-002 is not assigned to any unit",
    ]


def test_directory_created_by_a_dependency_is_fine(h: Harness) -> None:
    ok = units_text(unit("U1", [], ["FR-001", "AS-1"], ["newpkg/a.py", "newpkg/b.py"]),
                    unit("U2", ["U1"], ["FR-002", "AS-2"], ["newpkg/c.py"], risk="high"))
    h.scenario(slice=[{"text": ok}])
    r = h.run(None, "--assume-defaults")
    assert r.rc == 0 and h.stages() == ["questions", "slice"], r.out


def test_still_invalid_after_two_repairs_exits_1_and_keeps_the_draft(h: Harness) -> None:
    bad = units_text(unit("U1", [], ["FR-001", "AS-1"], ["src/store.py"], est=999))
    h.scenario(slice=[{"text": bad}], repair=[{"text": bad}])
    r = h.run(None, "--assume-defaults")
    assert r.rc == 1 and r.last.startswith("PREPARE: failed (draft still invalid after 2 repair rounds")
    assert h.stages() == ["questions", "slice", "repair", "repair"]
    assert "- U1: est_lines 999 exceeds" in r.out and "- requirement AS-2 is not assigned to any unit" in r.out
    assert (h.state_dir / "units.yaml").is_file() and not h.out.exists()


def test_resume_after_a_kill_between_stages(h: Harness) -> None:
    h.scenario(slice=[{"kill_driver": True}, {"text": GOOD}])
    r = h.run(None, "--assume-defaults")
    assert r.rc != 0 and h.stages() == ["questions", "slice"]
    assert h.state()["questions_done"] is True and not h.out.exists()
    r = h.run(None, "--assume-defaults")
    assert r.rc == 0, r.out + r.err
    assert h.stages() == ["questions", "slice", "slice"]
    assert h.state()["cost"]["sessions"] == 2


def test_max_usd_aborts_before_a_session_and_resumes(h: Harness) -> None:
    h.scenario(questions=[{"text": QTEXT, "cost": 3.0}], slice=[{"text": GOOD, "cost": 1.0}])
    r = h.run(None, "--assume-defaults", "--max-usd", "2")
    assert r.rc == 1 and "cost budget exhausted" in r.last and h.stages() == ["questions"]
    r = h.run(None, "--assume-defaults", "--max-usd", "10")
    assert r.rc == 0 and h.stages() == ["questions", "slice"]
    assert h.state()["cost"]["total_usd"] == pytest.approx(4.0)


def test_rate_limit_with_no_wait_exits_3_and_resumes(h: Harness) -> None:
    h.scenario(slice=[{"result": "rate_limit"}, {"text": GOOD}])
    r = h.run(None, "--assume-defaults", "--no-wait")
    assert r.rc == 3 and r.last == "PREPARE: rate limited (rerun to resume)"
    assert h.stages() == ["questions", "slice"] and not h.out.exists()
    r = h.run(None, "--assume-defaults", "--no-wait")
    assert r.rc == 0 and h.stages() == ["questions", "slice", "slice"]


def test_idea_text_input(h: Harness) -> None:
    idea = "Add a command that exports the ledger to CSV"
    h.scenario(questions=[{"text": "Q1 [info]: Delimiter? | default: comma | cost of a wrong guess: none"}],
               slice=[{"text": units_text(unit("U1", [], [], ["src/store.py"]))}])
    r = h.run(idea)
    assert r.rc == 0, r.out + r.err
    assert h.state()["lint"] == "skipped" and h.stages() == ["questions", "slice"]
    assert (h.state_dir / "spec.md").read_text(encoding="utf-8") == idea
    assert h.state_dir.name.startswith("idea-")
    text = h.out.read_text(encoding="utf-8")
    assert "# requirements covered: 0/0" in text and "# questions: answered 0, defaulted 0" in text
    assert h.queue_errors() == []


def test_dry_run_runs_lint_and_base_only(h: Harness) -> None:
    r = h.run(None, "--dry-run")
    assert r.rc == 0 and r.last == "PREPARE: dry-run ok" and "gates: no approved profile" in r.out
    assert h.calls() == [] and not h.out.exists()
    assert not (h.repo / ".powerpuff").exists()


def test_not_a_repo(tmp_path: Path) -> None:
    p = subprocess.run([sys.executable, str(DRIVER), "idea", "--repo", str(tmp_path)], capture_output=True, text=True)
    assert p.returncode == 1 and "not a git repository" in p.stdout
