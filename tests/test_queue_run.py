"""Tests for scripts/queue_run.py: throwaway git repos, a scripted stand-in for the claude CLI on PATH (never the real
one). The stand-in is defined here: its behaviour is driven by `FAKE <directive>` lines in the unit spec text."""

import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
QUEUE = ROOT / "scripts" / "queue_run.py"
sys.path.insert(0, str(ROOT / "scripts"))
import queue_run  # noqa: E402

FAKE_SRC = r'''
import json, os, re, subprocess, sys, time
from pathlib import Path

HEADS = [("You are the spec auditor", "audit"), ("You write the acceptance tests", "tests"),
         ("You complete the acceptance tests", "topup"), ("You implement a spec", "implement"),
         ("The acceptance tests pass without any implementation", "vacuous"),
         ("The deterministic check failed", "fix"), ("You are the independent acceptance reviewer", "accept"),
         ("The independent acceptance review found gaps", "gapfix"), ("Not finished", "retry")]
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
argv = sys.argv[1:]
model = argv[argv.index("--model") + 1] if "--model" in argv else None
prompt = sys.stdin.read()
first = prompt.lstrip().splitlines()[0] if prompt.strip() else ""
stage = next((n for h, n in HEADS if first.startswith(h)), "unknown")
sp = Path(".forge/spec.md")
spec = sp.read_text(encoding="utf-8") if sp.is_file() else ""
m = re.match(r"# (\S+):", spec)
uid = m.group(1) if m else "?"
dirs = {k: v for k, v in re.findall(r"^FAKE (\w+)(?:=(\S+))?", spec, re.M)}
state = Path(os.environ["FAKE_STATE"])
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"uid": uid, "stage": stage, "model": model, "cwd": os.getcwd()}) + "\n")


def once(name):
    marker = state / f"{name}_{uid}"
    if marker.exists():
        return False
    marker.write_text("1", encoding="utf-8")
    return True


def put(rel, text):
    p = Path(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


out_file = f"out_{uid}.txt"
cost = float(dirs.get("cost") or 0.25)
kind = "ok"
if stage == "audit":
    ask = ("Q2 [blocking]: which db? | default: sqlite | cost of a wrong guess: rewrite\n" if "ask" in dirs else "")
    put(".forge/requirements.md", f"R1: {uid} creates {out_file}\nD1: naming -> out_<id> - convention\nQ1: more?\n{ask}")
    put(".forge/digest.md", "digest\n")
    gate = "python -c \"import os,sys; sys.exit(1 if os.path.exists('broken.flag') else 0)\""
    put(".forge/gates.json", json.dumps({"format_fix": None, "gates": {"unit": gate}}))
elif stage == "tests":
    put(f"tests/test_acceptance_{uid}.py",
        f"import os\n\n\ndef test_r1():\n    # R1\n    assert os.path.exists('{out_file}')\n")
    put(".forge/acceptance-files.txt", f"tests/test_acceptance_{uid}.py\n")
elif stage in ("implement", "fix", "gapfix"):
    if "hang_once" in dirs and once("hang"):
        time.sleep(300)
    if "rate_once" in dirs and once("rate"):
        kind, cost = "rate_limit", 0
    else:
        put(out_file, "ok\n")
        if "fail" in dirs or ("fail_once" in dirs and "Previous attempt" not in spec):
            put("broken.flag", "1\n")
        if "shared" in dirs:
            put("shared.txt", "unit side\n")
        if "push" in dirs and once("push"):
            integ = Path(os.getcwd()).parent / "_integration"
            (integ / "shared.txt").write_text("integration side\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=integ, check=True)
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--no-gpg-sign",
                            "-m", "side"], cwd=integ, check=True)
elif stage == "accept":
    put(".forge/gaps.json", json.dumps({"gaps": [], "questions": []}))
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s"}), flush=True)
is_error = kind == "rate_limit"
text = f"Claude AI usage limit reached|{int(time.time()) + 5}" if is_error else "done"
print(json.dumps({"type": "result", "subtype": "error" if is_error else "success", "is_error": is_error,
                  "result": text, "total_cost_usd": cost, "num_turns": 3, "session_id": "s",
                  "modelUsage": {model: {"costUSD": cost}}}), flush=True)
'''

IDENT = ["-c", "user.name=t", "-c", "user.email=t@t"]


class Res:
    def __init__(self, rc: int, out: str):
        self.rc, self.out = rc, out

    @property
    def last(self) -> str:
        return self.out.strip().splitlines()[-1]


def unit(uid: str, deps: tuple = (), risk: str = "low", fake: tuple = (), files: tuple | None = None) -> dict:
    spec = f"Build {uid}\n" + "".join(f"FAKE {d}\n" for d in fake)
    return {"id": uid, "title": f"title {uid}", "deps": list(deps), "risk": risk, "requirements": ["FR-1"],
            "files": list(files if files is not None else (f"src/{uid}.py",)), "spec": spec}


class QH:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.bindir = tmp / "bin"
        self.bindir.mkdir()
        fake = tmp / "fake_q.py"
        fake.write_text(FAKE_SRC, encoding="utf-8")
        (self.bindir / "claude.cmd").write_text(f'@"{sys.executable}" "{fake}" %*\r\n', encoding="ascii")
        sh = self.bindir / "claude"
        sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n', encoding="ascii")
        sh.chmod(sh.stat().st_mode | stat.S_IEXEC)
        self.fstate = tmp / "fstate"
        self.fstate.mkdir()
        self.log = tmp / "calls.jsonl"
        self.repo = tmp / "repo"
        self.repo.mkdir()
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git(*IDENT, "commit", "-q", "--no-gpg-sign", "-m", "init")
        self.n = 0
        self.run_dir = self.repo / ".powerpuff" / "runs" / "r1"

    def git(self, *args: str, cwd: Path | None = None) -> str:
        p = subprocess.run(["git", *args], cwd=cwd or self.repo, capture_output=True, text=True, encoding="utf-8")
        return p.stdout.strip()

    def queue(self, units: list[dict], **top: object) -> Path:
        q = {"run_id": "r1", "engine": "forge", "base": "main", "auto_merge": ["low"], "model": "strong", "writer": "writer",
             "budget": {"max_usd": 60, "max_hours": 0.5}, "units": units, **top}
        p = self.tmp / "queue.yaml"
        p.write_text(json.dumps(q, indent=1), encoding="utf-8")
        return p

    def env(self) -> dict:
        return {**os.environ, "PATH": str(self.bindir) + os.pathsep + os.environ["PATH"],
                "FAKE_STATE": str(self.fstate), "FAKE_LOG": str(self.log), "PYTHONUNBUFFERED": "1"}

    def popen(self, qfile: Path, *args: str) -> tuple[subprocess.Popen, Path]:
        self.n += 1
        out = self.tmp / f"out{self.n}.txt"
        kw = {"start_new_session": True} if sys.platform != "win32" else {}
        p = subprocess.Popen([sys.executable, str(QUEUE), str(qfile), "--repo", str(self.repo), *args],
                             cwd=self.tmp, env=self.env(), stdout=open(out, "w", encoding="utf-8"),
                             stderr=subprocess.STDOUT, **kw)
        return p, out

    def run(self, qfile: Path, *args: str, timeout: int = 240) -> Res:
        p, out = self.popen(qfile, *args)
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(p)
            raise
        return Res(rc, out.read_text(encoding="utf-8"))

    def calls(self) -> list[dict]:
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()] if self.log.is_file() else []

    def stages(self, uid: str) -> list[str]:
        return [c["stage"] for c in self.calls() if c["uid"] == uid]

    def state(self) -> dict:
        return json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))

    def statuses(self) -> dict[str, str]:
        return {k: v["status"] for k, v in self.state()["units"].items()}

    def summary(self) -> dict:
        return json.loads((self.run_dir / "summary.json").read_text(encoding="utf-8"))

    def tree(self, branch: str) -> set[str]:
        return set(self.git("ls-tree", "-r", "--name-only", branch).splitlines())


def kill_tree(p: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    else:
        os.killpg(p.pid, signal.SIGKILL)
    p.wait()


@pytest.fixture
def h(tmp_path: Path) -> QH:
    return QH(tmp_path)


IB = "powerpuff/run-r1"


@pytest.mark.parametrize("name", ["cycle", "unknown_dep", "overlap", "duplicate", "no_spec", "dirty", "bad_base"])
def test_validation_errors(h: QH, name: str) -> None:
    a, b = unit("U1", files=("src/a.py",)), unit("U2", files=("src/a.py",))
    extra: dict = {}
    if name == "cycle":
        a["deps"], b["deps"], b["files"] = ["U2"], ["U1"], []
    elif name == "unknown_dep":
        a["deps"], b["files"] = ["U9"], []
    elif name == "duplicate":
        b["id"], b["files"] = "U1", []
    elif name == "no_spec":
        a["spec"], b["files"] = "  ", []
    elif name == "dirty":
        b["files"] = []
        (h.repo / "README.md").write_text("changed\n", encoding="utf-8")
    elif name == "bad_base":
        b["files"], extra = [], {"base": "nope"}
    r = h.run(h.queue([a, b], **extra))
    assert r.rc == 3, r.out
    assert "QUEUE: config error" in r.out
    keys = {"cycle": "cycle", "unknown_dep": "unknown dep U9", "overlap": "U1 and U2 share src/a.py",
            "duplicate": "duplicate unit id U1", "no_spec": "no spec text", "dirty": "uncommitted changes",
            "bad_base": "base 'nope' does not exist"}
    assert keys[name] in r.out
    assert not (h.repo / ".powerpuff").exists()
    assert h.git("branch", "--list", "powerpuff/*") == ""
    assert h.git("worktree", "list").count("\n") == 0


def test_all_problems_listed_at_once(h: QH) -> None:
    a, b = unit("U1", deps=("U9",), files=("x",)), unit("U2", files=("x",))
    b["spec"] = ""
    r = h.run(h.queue([a, b]))
    assert r.rc == 3
    assert r.out.count("\n- ") >= 3


def test_dependent_sharing_a_file_is_allowed(h: QH) -> None:
    r = h.run(h.queue([unit("U1", files=("a",)), unit("U2", deps=("U1",), files=("a",))]), "--dry-run")
    assert r.rc == 0, r.out


def test_dry_run_creates_nothing(h: QH) -> None:
    units = [unit("U1"), unit("U2", deps=("U1",), risk="high"), unit("U3")]
    r = h.run(h.queue(units, budget={"max_usd": 60, "max_hours": 8}), "--dry-run")
    assert r.rc == 0, r.out
    assert r.last == "QUEUE: dry-run ok"
    assert "first_pass=$40.20" in r.out and "unit_cap=$26.80" in r.out
    assert [x.split()[2] for x in r.out.splitlines() if x.startswith("queue: ") and ". U" in x] == ["U1", "U2", "U3"]
    assert "U2 risk=high (awaits audit)" in r.out and "U1 risk=low (auto-merge)" in r.out
    only = h.run(h.queue(units), "--dry-run", "--only", "U3")
    assert only.out.count(" risk=") == 1 and "U3" in only.out
    assert not (h.repo / ".powerpuff").exists()
    assert h.git("branch", "--list", "powerpuff/*") == "" and not h.calls()


def test_happy_path(h: QH) -> None:
    before = h.git("rev-parse", "HEAD")
    final = {"commands": {"has_u3": "python -c \"import os,sys; sys.exit(0 if os.path.exists('out_U3.txt') else 1)\""}}
    q = h.queue([unit("U1"), unit("U2", deps=("U1",)), unit("U3")], final=final)
    r = h.run(q)
    assert r.rc == 0, r.out
    assert r.last == "QUEUE: done"
    assert h.statuses() == {"U1": "merged", "U2": "merged", "U3": "merged"}
    assert {"out_U1.txt", "out_U2.txt", "out_U3.txt"} <= h.tree(IB)
    assert h.git("log", "--merges", "--format=%s", IB).count("powerpuff: merge") == 3
    assert "out_U1.txt" in h.tree("powerpuff/r1/U2")
    assert h.git("rev-parse", "HEAD") == before and h.git("branch", "--show-current") == "main"
    assert h.git("status", "--porcelain") == ""
    assert not list(h.repo.glob("out_*.txt")) and not (h.repo / ".forge").exists()
    wts = h.run_dir / "wt"
    assert not (wts / "U1").exists() and not (wts / "U3").exists() and not (wts / "_integration").exists()
    assert h.git("branch", "--list", "powerpuff/r1/*").count("powerpuff/r1/U") == 3
    assert [c["stage"] for c in h.calls() if c["uid"] == "U1"] == ["audit", "tests", "implement", "accept"]
    st = h.state()
    assert st["units"]["U1"]["cost"]["total_usd"] == pytest.approx(1.0)
    assert st["units"]["U1"]["forge_status"] == "done" and st["units"]["U1"]["worktree"]
    s = h.summary()
    assert s["status"] == "done" and s["cost"]["total_usd"] == pytest.approx(3.0)
    assert s["cost"]["by_model"] == {"strong": pytest.approx(1.5), "writer": pytest.approx(1.5)}
    assert s["final"]["has_u3"]["ok"] is True
    assert (h.run_dir / "SUMMARY.md").read_text(encoding="utf-8").count("| merged |") == 3
    n = len(h.calls())
    again = h.run(q)
    assert again.rc == 0 and again.last == "QUEUE: done" and len(h.calls()) == n


def test_failure_blocks_dependents_only(h: QH) -> None:
    units = [unit("U1", fake=("fail",)), unit("U2", deps=("U1",)), unit("U3"), unit("U4", risk="high"),
             unit("U5", deps=("U4",))]
    r = h.run(h.queue(units, max_attempts=1))
    assert r.rc == 2, r.out
    assert r.last == "QUEUE: partial"
    assert h.statuses() == {"U1": "failed", "U2": "blocked", "U3": "merged", "U4": "done", "U5": "blocked"}
    st = h.state()["units"]
    assert st["U1"]["reason"] and "FAILED" in st["U1"]["reason"]
    assert st["U2"]["reason"] == "dep U1 failed" and st["U5"]["reason"] == "awaits audit of U4"
    assert "out_U3.txt" in h.tree(IB) and "out_U4.txt" not in h.tree(IB)
    assert h.stages("U2") == [] and h.stages("U5") == []
    assert (h.run_dir / "wt" / "U4").is_dir() and (h.run_dir / "wt" / "U1").is_dir()
    s = h.summary()
    assert s["status"] == "partial" and s["awaiting_audit"] == ["U4"]
    assert {b["id"]: b["reason"] for b in s["blocked"]} == {"U2": "dep U1 failed", "U5": "awaits audit of U4"}
    keys = {"id", "title", "status", "risk", "deps", "branch", "worktree", "forge_status", "cost", "started",
            "finished", "reason", "minutes", "report", "decisions", "questions", "open_gaps", "guard"}
    by = {u["id"]: u for u in s["units"]}
    assert all(keys <= set(u) for u in s["units"])
    assert by["U3"]["decisions"] == ["D1: naming -> out_<id> - convention"] and by["U3"]["questions"][0].startswith("Q1")
    assert Path(by["U3"]["report"]).is_file() and Path(by["U4"]["report"]).is_file()
    assert by["U4"]["guard"]["ok"] is True and by["U4"]["open_gaps"] == []
    assert by["U1"]["report"] and by["U2"]["report"] is None
    md = (h.run_dir / "SUMMARY.md").read_text(encoding="utf-8")
    assert "| unit | status | risk | cost | min | branch | reason |" in md
    assert "## Awaiting audit\n- U4" in md and "- U2: dep U1 failed" in md


def test_blocking_question_needs_answer_then_resumes(h: QH) -> None:
    q = h.queue([unit("U1", fake=("ask",)), unit("U2", deps=("U1",)), unit("U3")])
    r = h.run(q)
    assert r.rc == 2 and r.last == "QUEUE: partial", r.out
    assert h.statuses() == {"U1": "needs-answer", "U2": "blocked", "U3": "merged"}
    st = h.state()["units"]
    qpath = h.run_dir / "wt" / "U1" / ".forge" / "questions.json"
    assert "blocking questions: Q2" in st["U1"]["reason"] and "; answer in " in st["U1"]["reason"]
    assert st["U1"]["reason"].endswith("questions.json and rerun")
    assert st["U2"]["reason"] == "dep U1 needs-answer"
    assert [x["id"] for x in h.summary()["needs_answer"]] == ["U1"]
    assert "## Needs answer" in (h.run_dir / "SUMMARY.md").read_text(encoding="utf-8")
    assert h.stages("U1") == ["audit"] and h.stages("U2") == []
    qs = json.loads(qpath.read_text(encoding="utf-8"))
    for x in qs:
        if x["id"] == "Q2":
            x["answer"] = "postgres"
    qpath.write_text(json.dumps(qs), encoding="utf-8")
    r2 = h.run(q)
    assert r2.rc == 0 and r2.last == "QUEUE: done", r2.out
    assert h.statuses() == {"U1": "merged", "U2": "merged", "U3": "merged"}
    assert h.stages("U1").count("audit") == 1


def test_assume_defaults_merges_despite_blocking_question(h: QH) -> None:
    r = h.run(h.queue([unit("U1", fake=("ask",))], assume_defaults=True))
    assert r.rc == 0, r.out
    assert h.statuses() == {"U1": "merged"}


def test_assume_defaults_must_be_bool(h: QH) -> None:
    r = h.run(h.queue([unit("U1")], assume_defaults="yes"), "--dry-run")
    assert r.rc == 3 and "assume_defaults" in r.out


def test_merge_conflict_fails_unit_and_continues(h: QH) -> None:
    r = h.run(h.queue([unit("U1", fake=("shared", "push=x")), unit("U2")], max_attempts=1))
    assert r.rc == 2, r.out
    assert h.statuses() == {"U1": "failed", "U2": "merged"}
    assert h.state()["units"]["U1"]["reason"] == "merge conflict"
    assert "powerpuff/r1/U1" in h.git("branch", "--list", "powerpuff/r1/U1")
    assert (h.run_dir / "wt" / "U1").is_dir()
    assert h.git("status", "--porcelain", cwd=h.run_dir / "wt" / "U1") == ""
    assert h.tree(IB) >= {"out_U2.txt", "shared.txt"} and "out_U1.txt" not in h.tree(IB)
    assert h.git("show", f"{IB}:shared.txt") == "integration side"


def test_resume_after_queue_killed_mid_unit(h: QH) -> None:
    q = h.queue([unit("U1"), unit("U2", fake=("hang_once",))])
    p, out = h.popen(q)
    marker = h.fstate / "hang_U2"
    deadline = time.monotonic() + 150
    while not marker.exists() and time.monotonic() < deadline and p.poll() is None:
        time.sleep(0.2)
    assert marker.exists(), out.read_text(encoding="utf-8")
    kill_tree(p)
    assert h.statuses() == {"U1": "merged", "U2": "running"}
    r = h.run(q)
    assert r.rc == 0 and r.last == "QUEUE: done", r.out
    assert h.statuses() == {"U1": "merged", "U2": "merged"}
    assert h.stages("U1") == ["audit", "tests", "implement", "accept"]
    assert h.stages("U2") == ["audit", "tests", "implement", "implement", "accept"]
    assert len({c["cwd"] for c in h.calls() if c["uid"] == "U2"}) == 1


def test_rate_limit_no_wait_stops_then_resumes(h: QH) -> None:
    q = h.queue([unit("U1", fake=("rate_once",)), unit("U2")])
    r = h.run(q, "--no-wait")
    assert r.rc == 3, r.out
    assert not any(x.startswith("QUEUE:") for x in r.out.splitlines())
    assert h.statuses() == {"U1": "running", "U2": "pending"} and h.state()["units"]["U1"]["reason"] == "rate limit"
    assert h.stages("U2") == []
    assert (h.run_dir / "summary.json").is_file()
    r2 = h.run(q, "--no-wait")
    assert r2.rc == 0 and r2.last == "QUEUE: done", r2.out
    assert h.statuses() == {"U1": "merged", "U2": "merged"}
    assert h.stages("U1").count("audit") == 1 and h.stages("U1").count("tests") == 1


def test_rate_limit_waits_by_reset_time(h: QH, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    slept: list[float] = []
    monkeypatch.setattr(queue_run, "sleep", slept.append)
    for k, v in h.env().items():
        monkeypatch.setenv(k, v)
    rc = queue_run.main([str(h.queue([unit("U1", fake=("rate_once",))])), "--repo", str(h.repo)])
    assert rc == 0, capsys.readouterr().out
    assert len(slept) == 1 and 60 <= slept[0] <= 130
    assert h.statuses() == {"U1": "merged"} and h.stages("U1").count("audit") == 1


@pytest.mark.parametrize("always", [False, True])
def test_crash_is_retried_once(h: QH, monkeypatch: pytest.MonkeyPatch, always: bool) -> None:
    orig, calls = queue_run.Queue.forge_cmd, []

    def cmd(self, spec, wt, cap):
        calls.append(1)
        return [sys.executable, "-c", "import sys; sys.exit(7)"] if always or len(calls) == 1 else orig(self, spec, wt, cap)
    monkeypatch.setattr(queue_run.Queue, "forge_cmd", cmd)
    for k, v in h.env().items():
        monkeypatch.setenv(k, v)
    rc = queue_run.main([str(h.queue([unit("U1")])), "--repo", str(h.repo)])
    assert len(calls) == 2
    if always:
        assert rc == 1 and h.statuses() == {"U1": "failed"}
        assert h.state()["units"]["U1"]["reason"] == "forge exited with code 7 twice"
    else:
        assert rc == 0 and h.statuses() == {"U1": "merged"}


def test_budget_exhaustion_skips_remaining(h: QH) -> None:
    q = h.queue([unit("U1"), unit("U2"), unit("U3")], budget={"max_usd": 3, "max_hours": 0.5})
    r = h.run(q)
    assert r.rc == 2, r.out
    assert h.statuses() == {"U1": "merged", "U2": "merged", "U3": "skipped-budget"}
    assert "cannot cover" in h.state()["units"]["U3"]["reason"]
    assert h.stages("U3") == []
    assert h.summary()["status"] == "partial"


def test_attended_pauses_after_each_unit(h: QH) -> None:
    q = h.queue([unit("U1"), unit("U2")])
    r = h.run(q, "--attended")
    assert r.rc == 4 and r.last == "QUEUE: paused after U1", r.out
    assert h.statuses() == {"U1": "merged", "U2": "pending"}
    r2 = h.run(q, "--attended")
    assert r2.rc == 0 and r2.last == "QUEUE: done", r2.out
    assert h.stages("U1").count("audit") == 1


def test_final_commands_pass_and_fail(h: QH) -> None:
    final = {"commands": {"ok": "python -c \"import os,sys; sys.exit(0 if os.path.exists('out_U1.txt') else 1)\"",
                          "bad": "python -c \"print('boom'); import sys; sys.exit(5)\""}}
    r = h.run(h.queue([unit("U1")], final=final))
    assert r.rc == 2 and r.last == "QUEUE: partial", r.out
    assert h.statuses() == {"U1": "merged"}
    s = h.summary()
    assert s["final"]["ok"]["ok"] is True
    assert s["final"]["bad"] == {"ok": False, "code": 5, "tail": "boom"}
    md = (h.run_dir / "SUMMARY.md").read_text(encoding="utf-8")
    assert "- bad: FAIL (exit 5)" in md and "boom" in md


def test_no_unit_succeeds_is_failed(h: QH) -> None:
    r = h.run(h.queue([unit("U1", fake=("fail",))]))
    assert r.rc == 1 and r.last == "QUEUE: failed", r.out


def test_yaml_format_parses(h: QH) -> None:
    pytest.importorskip("yaml")
    q = h.tmp / "queue.yaml"
    q.write_text("run_id: r1\nbase: main\nauto_merge: [low]\nunits:\n  - id: U1\n    risk: low\n    files: [src/a.py]\n"
                 "    spec: |\n      Build it\n      FAKE cost=0.1\n", encoding="utf-8")
    r = h.run(q, "--dry-run")
    assert r.rc == 0 and "1. U1 risk=low" in r.out, r.out
