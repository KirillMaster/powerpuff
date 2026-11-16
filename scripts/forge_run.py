"""forge v2 - script-orchestrated spec -> program run. The orchestrator is this script, not a model.

Stages (state in .forge/state.json, resumable; every stage is one `claude -p` session):
  audit   (strong model)  spec -> .forge/requirements.md (R/D lines), digest.md, gates.json
  tests   (writer model)  acceptance tests from R/D, context never sees an implementation; frozen by sha256
  build   (writer model)  ONE session implements everything; forge_check decides; red -> resume same session (<=3),
                          then one strong-model escalation
  accept  (strong model)  ONE fresh pass over diff vs spec -> .forge/gaps.json; gaps -> writer fix round (<=2)
  report  (script)        .forge/REPORT.md, last stdout line `FORGE: done|failed`

Usage: python forge_run.py <spec-dir|spec-file|ticket text> [--repo .] [--model opus] [--writer sonnet]
       [--max-hours 8] [--max-usd 60] [--preflight] [--stream] [--no-wait]
  --stream   re-emit the sessions' stream-json on stdout and finish with one synthetic `result` event (bob-bench)
  --no-wait  on a rate limit emit the result and exit 3 instead of sleeping (the caller pauses and re-invokes)
Detached on Windows: start "" /b cmd /c "set PYTHONUNBUFFERED=1&& python forge_run.py specs/001 > forge.log 2>&1"
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_check  # noqa: E402

RATE = ("usage limit", "rate limit", "429", "overloaded")
EPOCH = re.compile(r"\|\s*(\d{10})\b")
CLOCK = re.compile(r"\bresets?(?:\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)\b", re.IGNORECASE)
SPEC_FILES = ("spec.yaml", "spec.md", "plan.yaml", "plan.md", "tasks.yaml", "tasks.md", "contracts.yaml",
              "data-model.yaml")

AUDIT = """You are the spec auditor of an unattended build. Do NOT write production code or tests.
Read `.forge/spec.md` (the full spec). Explore the repository only as far as needed (entry points, the modules the
spec touches, existing tests, conventions, build/test config).
Write exactly three files:
1. `.forge/requirements.md`
   - `R<n>: <atomic testable statement>` for every observable rule: endpoints/commands, status codes, validation and
     the ORDER of checks, state transitions, limits, computed values incl. rounding, interactions ("A after B ..."),
     existing behaviour that must keep working.
   - `D<n>: <ambiguity> -> <chosen reading> - <basis>` for every place that allows two readings or is silent. Choose
     by, in order: existing code/tests/conventions; literal wording; consistency with other clauses and with every
     example in the spec; the stricter/safer reading. When two readings are compatible, require both.
   - Re-read the spec once against your list hunting for: order of checks, boundary inclusivity, rounding, what
     resets vs carries over, side effects on related entities, time shifts. Each hit becomes an R or D line.
   - `Q<n>: <question>` only for decisions a wrong guess would make expensive; still give your default in a D line.
2. `.forge/digest.md` (<= 80 lines): stack, how to build/test, module map (path -> responsibility), conventions with
   path:line (error format, naming, test layout, fixtures), files this spec will touch.
3. `.forge/gates.json`: {"format_fix": "<command or null>", "gates": {"<name>": "<command>"}} - the format/lint/
   architecture/test commands that must exit 0 at the end, run from the repo root. Take them from
   `.claude/bob-pipeline.yaml` (tests.run_command, enabled tools of category lint/format/architecture) if present,
   else from the manifests (dotnet build -warnaserror / dotnet format --verify-no-changes / dotnet test; package.json
   lint/test scripts or npx tsc --noEmit; ruff check . / ruff format --check . / pytest -q). Only commands that work
   in this repo (install dependencies if needed and check the tools exist). There must be a test gate.
Finish with one line: AUDIT: done"""

TESTS = """You write the acceptance tests of an unattended build. Do NOT write production code.
Inputs: `.forge/spec.md`, `.forge/requirements.md` (R and D lines are binding), `.forge/digest.md`.
Write executable acceptance tests through the public interface (HTTP/CLI/public API) in the repo's test layout, in
new files whose names contain `acceptance`. Every R has at least one test; the test name or a comment contains its
id (`R7`). Assert exact values from the spec (statuses, numbers, order of checks, rounding). Black-box only: use the
documented entry points; when the implementation does not exist yet the tests must fail for that reason only (they
must be syntactically valid and collectable where the framework allows it).
Then write `.forge/acceptance-files.txt`: one repo-relative path per line, every acceptance test file.
Finish with one line: TESTS: done"""

BUILD = """You implement a spec in an unattended build. Inputs: `.forge/spec.md`, `.forge/requirements.md` (R and D
lines are binding decisions), `.forge/digest.md`, the frozen acceptance tests listed in `.forge/frozen.sha256`.
Rules:
- Implement everything so that every acceptance test passes; add unit tests for non-trivial rules.
- NEVER edit, delete or skip the frozen acceptance test files. If one contradicts the spec, do not work around it:
  append `- <test>: <why, with the spec quote>` to `.forge/disputes.md` and implement the spec.
- Follow existing conventions and structure; no unrelated refactors; never weaken a gate (no disabled rules, no
  skipped tests, no config edits).
- Before finishing run the formatter in fix mode (FORMAT_FIX) and then every gate until all exit 0:
GATES
Finish with one line: BUILD: done"""

FIX = """The deterministic check failed. Fix the causes (same rules: frozen tests untouched, gates never weakened),
re-run the gates until green, then finish with: BUILD: done

forge_check output:
"""

ACCEPT = """You are the independent acceptance reviewer of an unattended build. You did not write this code.
Inputs: `.forge/spec.md`, `.forge/requirements.md`, `.forge/disputes.md` (may be absent), the change
`git diff START_SHA` (use `git diff --stat START_SHA` first, then read what matters).
Find only correctness gaps: spec statements not implemented or implemented differently, D decisions contradicted,
R lines whose tests do not really assert the rule, broken existing behaviour. Skip style. Every gap needs evidence
(spec quote + file:line). Do not edit production code.
Disputes: decide each against the spec text. If a frozen acceptance test is wrong you MAY correct that test file
(nothing else) and must note it in `.forge/unfrozen.md` (`- <test>: <reason>`).
Write `.forge/gaps.json`: {"gaps": [{"id": "R7|new", "kind": "missing|wrong|untested|regression", "evidence":
"...", "fix": "..."}], "questions": ["<things only the human can decide>"]}.
Finish with one line: ACCEPT: done"""

GAPFIX = """The independent acceptance review found gaps. Fix every one (add tests for them in NEW files; frozen
acceptance tests stay untouched), re-run the formatter and all gates until green, finish with: BUILD: done

gaps:
"""


def reset_wait_s(text: str, fallback_min: float = 15) -> float:
    now = datetime.now(timezone.utc)
    if m := EPOCH.search(text):
        return max(60.0, int(m.group(1)) - now.timestamp() + 60)
    if m := CLOCK.search(text):
        h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        local = datetime.now().astimezone()
        at = local.replace(hour=h, minute=int(m.group(2) or 0), second=0, microsecond=0)
        if at <= local:
            at += timedelta(days=1)
        return (at - local).total_seconds() + 60
    return fallback_min * 60


class RateLimited(Exception):
    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


class Run:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.root = Path(a.repo).resolve()
        self.forge = self.root / ".forge"
        self.forge.mkdir(exist_ok=True)
        self.state_path = self.forge / "state.json"
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.is_file() else {}
        self.state.setdefault("stage", "init")
        self.state.setdefault("cost", {"total_usd": 0.0, "by_model": {}, "by_stage": {}, "sessions": 0, "turns": 0})
        self.deadline = time.monotonic() + a.max_hours * 3600

    # ---- plumbing ---------------------------------------------------------------------------------
    def log(self, msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        with open(self.forge / "progress.md", "a", encoding="utf-8") as f:
            f.write("- " + line + "\n")
        print(line, file=sys.stderr if self.a.stream else sys.stdout, flush=True)

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state, indent=1, ensure_ascii=False), encoding="utf-8")

    def git(self, *args: str) -> str:
        p = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        return p.stdout.strip()

    def commit(self, msg: str) -> None:
        self.git("add", "-A", "--", ".", ":!.forge")
        self.git("-c", "user.name=forge", "-c", "user.email=forge@local", "commit", "-q", "--no-gpg-sign", "-m", msg)

    def session(self, stage: str, model: str, prompt: str, resume: str | None = None) -> dict:
        """One `claude -p` session. Returns {"ok", "text", "session_id"}; raises RateLimited."""
        if time.monotonic() > self.deadline:
            raise SystemExit(self.finish("failed", "wall-clock budget exhausted"))
        if self.state["cost"]["total_usd"] >= self.a.max_usd:
            raise SystemExit(self.finish("failed", "cost budget exhausted"))
        argv = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", model,
                "--permission-mode", self.a.permission_mode]
        argv += ["--resume", resume] if resume else []
        self.log(f"{stage}: session start ({model}{', resume' if resume else ''})")
        proc = subprocess.Popen(argv, cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                shell=sys.platform == "win32")
        assert proc.stdin and proc.stdout
        proc.stdin.write(prompt)
        proc.stdin.close()
        result, tail = None, []
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                tail = (tail + [line.strip()])[-20:]
                continue
            if isinstance(ev, dict) and ev.get("type") == "result":
                result = ev
                continue  # not forwarded: the caller gets ONE synthetic result at the end
            if self.a.stream:
                sys.stdout.write(line if line.endswith("\n") else line + "\n")
                sys.stdout.flush()
        proc.wait()
        r = result or {}
        cost = self.state["cost"]
        spent = float(r.get("total_cost_usd") or 0)
        cost["total_usd"] = round(cost["total_usd"] + spent, 4)
        cost["by_stage"][stage] = round(cost["by_stage"].get(stage, 0) + spent, 4)
        cost["sessions"] += 1
        cost["turns"] += int(r.get("num_turns") or 0)
        for m, u in (r.get("modelUsage") or {}).items():
            cost["by_model"][m] = round(cost["by_model"].get(m, 0) + float(u.get("costUSD") or 0), 4)
        self.save()
        raw = r.get("result")
        text: str = raw if isinstance(raw, str) else ""
        low = (text + "\n" + "\n".join(tail)).lower()
        if (not result or r.get("is_error")) and any(p in low for p in RATE):
            raise RateLimited(text + "\n" + "\n".join(tail))
        ok = bool(result) and not r.get("is_error")
        self.log(f"{stage}: session end ok={ok} cost={spent:.2f} turns={r.get('num_turns')}"
                 + ("" if ok else " tail=" + " | ".join(tail[-3:])[:300]))
        return {"ok": ok, "text": text, "session_id": r.get("session_id")}

    def staged(self, stage: str, model: str, prompt: str, done, resume: str | None = None, tries: int = 2) -> dict:
        """Runs a session until `done()` holds (deterministic check), sleeping through rate limits."""
        out: dict = {"ok": False, "text": "", "session_id": resume}
        left = tries
        while left > 0:
            try:
                out = self.session(stage, model, prompt, resume)
            except RateLimited as e:
                if self.a.no_wait:
                    self.emit_result("rate limit: " + e.text[-300:], is_error=True)
                    raise SystemExit(3) from None
                wait = min(reset_wait_s(e.text), max(0.0, self.deadline - time.monotonic()))
                self.log(f"{stage}: rate limited, sleeping {wait / 60:.0f} min")
                time.sleep(wait)
                continue  # a rate limit does not consume a try
            left -= 1
            if out["ok"] and done():
                return out
            if out["ok"]:
                resume = out.get("session_id")
                prompt = "Not finished: the required output files are missing or invalid. Complete the task."
        return out

    def emit_result(self, text: str, is_error: bool = False) -> None:
        if not self.a.stream:
            return
        c = self.state["cost"]
        ev = {"type": "result", "subtype": "success", "is_error": is_error, "result": text,
              "total_cost_usd": c["total_usd"], "num_turns": c["turns"], "session_id": None,
              "modelUsage": {m: {"costUSD": v} for m, v in c["by_model"].items()}}
        sys.stdout.write(json.dumps(ev) + "\n")
        sys.stdout.flush()

    # ---- stages -----------------------------------------------------------------------------------
    def init(self) -> None:
        src = Path(self.a.spec) if len(self.a.spec) < 260 and "\n" not in self.a.spec else None
        if src and src.is_dir():
            parts = [f"# {n}\n\n{(src / n).read_text(encoding='utf-8')}" for n in SPEC_FILES if (src / n).is_file()]
            text = "\n\n".join(parts)
        elif src and src.is_file():
            text = src.read_text(encoding="utf-8")
        else:
            text = self.a.spec
        if not text.strip():
            raise SystemExit(self.finish("failed", "empty spec"))
        (self.forge / "spec.md").write_text(text, encoding="utf-8")
        if not (self.root / ".git").exists():
            self.git("init", "-q")
        if not self.git("rev-parse", "--verify", "-q", "HEAD"):
            self.git("-c", "user.name=forge", "-c", "user.email=forge@local", "commit", "-q", "--no-gpg-sign",
                     "--allow-empty", "-m", "forge: start")
        git_dir = Path(self.git("rev-parse", "--absolute-git-dir") or self.root / ".git")
        exclude = git_dir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        cur = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        if ".forge/" not in cur:
            exclude.write_text(cur + "\n.forge/\n", encoding="utf-8")
        self.state.update(stage="audit", start_sha=self.git("rev-parse", "HEAD"), writer=self.a.writer)
        self.save()

    def gates_cfg(self) -> dict:
        try:
            cfg = json.loads((self.forge / "gates.json").read_text(encoding="utf-8"))
            return cfg if isinstance(cfg, dict) else {}
        except (OSError, ValueError):
            return {}

    def audit(self) -> None:
        def done() -> bool:
            req = self.forge / "requirements.md"
            gates = self.gates_cfg().get("gates")
            return req.is_file() and bool(forge_check.R_LINE.search(req.read_text(encoding="utf-8"))) \
                and isinstance(gates, dict) and bool(gates)
        self.staged("audit", self.a.model, AUDIT, done)
        if not done():
            raise SystemExit(self.finish("failed", "audit produced no requirements/gates"))
        self.state["gates"] = self.gates_cfg()["gates"]
        self.state["stage"] = "tests"
        self.save()

    def tests(self) -> None:
        listing = self.forge / "acceptance-files.txt"

        def files() -> list[str]:
            if not listing.is_file():
                return []
            return [x.strip().replace("\\", "/") for x in listing.read_text(encoding="utf-8").splitlines()
                    if x.strip() and (self.root / x.strip()).is_file()]
        self.staged("tests", self.state["writer"], TESTS, lambda: bool(files()))
        if not files():
            raise SystemExit(self.finish("failed", "no acceptance tests written"))
        self.freeze(files())
        self.commit("forge: acceptance tests")
        self.state["stage"] = "build"
        self.save()

    def freeze(self, files: list[str]) -> None:
        lines = [f"{hashlib.sha256((self.root / f).read_bytes()).hexdigest()}  {f}" for f in files]
        (self.forge / "frozen.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def check(self) -> dict:
        files = forge_check.frozen_files(self.root)
        tampered = forge_check.check_frozen(self.root, files)["changed_or_missing"]
        for f in tampered:  # a tampered frozen test is restored before the gates run, never accepted
            self.git("checkout", "HEAD", "--", f)
        rep: dict = {"coverage": forge_check.check_coverage(self.root, files),
                     "frozen": forge_check.check_frozen(self.root, files),
                     "gates": forge_check.check_gates(self.root, self.a.gate_timeout)}
        if tampered:
            rep["frozen"] = {"ok": False, "files": len(files), "changed_or_missing": tampered,
                             "note": "restored from git; frozen acceptance tests must not be edited"}
        rep["done"] = all(rep[k]["ok"] for k in ("coverage", "frozen", "gates"))
        (self.forge / "check.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
        gates = {k: v["ok"] for k, v in rep["gates"].get("results", {}).items()}
        self.log(f"check: done={rep['done']} coverage={rep['coverage']['ok']} frozen={rep['frozen']['ok']} "
                 f"gates={gates}")
        return rep

    def build_prompt(self) -> str:
        g = self.gates_cfg()
        gates = "\n".join(f"  - {n}: `{c}`" for n, c in (g.get("gates") or {}).items())
        return BUILD.replace("FORMAT_FIX", str(g.get("format_fix") or "none")).replace("GATES", gates)

    def build_loop(self, stage: str, prompt: str, rounds: int) -> bool:
        key = f"sid_{stage}"
        sid = self.state.get(key)
        start = int(self.state.get(f"round_{stage}", 0))
        if start and not sid:
            prompt = self.build_prompt() + "\n\nA previous session was interrupted; inspect the tree and continue."
        for i in range(start, rounds):
            out = self.staged(f"{stage}{i + 1}", self.state["writer"], prompt, lambda: True, resume=sid, tries=2)
            sid = out.get("session_id") if out["ok"] else None
            self.commit(f"forge({stage}{i + 1})")
            rep = self.check()
            self.state[key] = sid
            self.state[f"round_{stage}"] = i + 1
            self.save()
            if rep["done"]:
                return True
            prompt = FIX + json.dumps(rep, ensure_ascii=False)[:4000]
            if not sid:
                prompt = self.build_prompt() + "\n\n" + prompt
        if self.state["writer"] != self.a.model and not self.state.get(f"esc_{stage}"):
            self.log(f"{stage}: escalation to {self.a.model}")
            self.state[f"esc_{stage}"] = True
            self.save()
            failing = (self.forge / "check.json").read_text(encoding="utf-8")[:4000] \
                if (self.forge / "check.json").is_file() else ""
            self.staged(f"{stage}-escalation", self.a.model,
                        self.build_prompt() + "\n\nA previous implementer left this failing:\n" + failing,
                        lambda: True)
            self.commit(f"forge({stage}-escalation)")
            return self.check()["done"]
        return False

    def build(self) -> None:
        ok = self.build_loop("build", self.build_prompt(), 3)
        self.state["build_green"] = ok
        self.state["stage"] = "accept"
        self.save()

    def accept(self) -> None:
        gaps_path = self.forge / "gaps.json"

        def gaps() -> dict | None:
            try:
                data = json.loads(gaps_path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else None
            except (OSError, ValueError):
                return None
        prompt = ACCEPT.replace("START_SHA", self.state["start_sha"])
        for rnd in range(int(self.state.get("accept_round", 0)), 3):
            gaps_path.unlink(missing_ok=True)
            self.staged(f"accept{rnd + 1}", self.a.model, prompt, lambda: gaps() is not None)
            files = list(forge_check.frozen_files(self.root))
            if (self.forge / "unfrozen.md").is_file() and files:  # reviewer corrected a wrong frozen test
                self.freeze(files)
                self.commit("forge: acceptance tests corrected by reviewer")
            data = gaps()
            found = data.get("gaps") or [] if data else [{"id": "new", "kind": "missing",
                                                          "evidence": "acceptance review produced no gaps.json"}]
            self.state["open_gaps"] = found
            self.state["questions"] = (data or {}).get("questions") or []
            self.state["accept_round"] = rnd + 1
            self.save()
            if not found or not data or rnd == 2:
                break
            self.log(f"accept: {len(found)} gap(s), fix round {rnd + 1}")
            self.build_loop(f"gapfix{rnd + 1}-", GAPFIX + json.dumps(found, ensure_ascii=False)[:6000], 2)
        self.state["stage"] = "report"
        self.save()

    def finish(self, status: str, why: str = "") -> int:
        rep: dict = self.check() if (self.forge / "frozen.sha256").is_file() else {"done": False}
        open_gaps = self.state.get("open_gaps") or []
        if status == "done" and not rep["done"]:
            status, why = "failed", why or "forge_check is red"
        if status == "done" and open_gaps:
            status, why = "failed", why or f"{len(open_gaps)} open acceptance gap(s)"
        req_path = self.forge / "requirements.md"
        req = req_path.read_text(encoding="utf-8") if req_path.is_file() else ""

        def pick(prefix: str) -> list[str]:
            return [x.strip() for x in req.splitlines() if re.match(rf"\s*[-*]?\s*\**{prefix}\d+", x)]

        def extra(name: str) -> str:
            p = self.forge / name
            return p.read_text(encoding="utf-8").strip() if p.is_file() else "none"

        c = self.state["cost"]
        gates = ", ".join(f"{k}={'pass' if v['ok'] else 'FAIL'}"
                          for k, v in (rep.get("gates", {}).get("results") or {}).items())
        lines = [f"# FORGE REPORT - {status.upper()}" + (f" ({why})" if why else ""), "",
                 f"- requirements: {len(pick('R'))}, uncovered: {rep.get('coverage', {}).get('uncovered', 'n/a')}",
                 f"- frozen tests intact: {rep.get('frozen', {}).get('ok', 'n/a')}",
                 f"- gates: {gates or 'n/a'}",
                 f"- cost (notional USD): {c['total_usd']} by model {c['by_model']} by stage {c['by_stage']}; "
                 f"sessions {c['sessions']}", "",
                 "## Decisions where the spec was silent (review these)", *(pick("D") or ["none"]), "",
                 "## Questions for the human", *((pick("Q") + (self.state.get("questions") or [])) or ["none"]), "",
                 "## Open acceptance gaps", *([json.dumps(g, ensure_ascii=False) for g in open_gaps] or ["none"]), "",
                 "## Disputed / corrected acceptance tests", extra("disputes.md"), extra("unfrozen.md"), ""]
        (self.forge / "REPORT.md").write_text("\n".join(str(x) for x in lines), encoding="utf-8")
        self.state["stage"] = "finished"
        self.state["status"] = status
        self.save()
        self.log(f"FORGE: {status} {why}")
        self.emit_result(f"report: .forge/REPORT.md\nFORGE: {status}")
        if not self.a.stream:
            print(f"FORGE: {status}", flush=True)
        return 0 if status == "done" else 1

    def run(self) -> int:
        if self.state["stage"] == "finished":
            status = self.state.get("status", "failed")
            self.emit_result(f"report: .forge/REPORT.md\nFORGE: {status}")
            if not self.a.stream:
                print(f"FORGE: {status}", flush=True)
            return 0 if status == "done" else 1
        if self.state["stage"] == "init":
            self.init()
        if self.state["stage"] == "audit":
            self.audit()
            if self.a.preflight:
                self.log("preflight: answer Q lines in .forge/requirements.md as `D<n> (human): ...`, then rerun")
                print("FORGE: preflight", flush=True)
                return 0
        if self.state["stage"] == "tests":
            self.tests()
        if self.state["stage"] == "build":
            self.build()
        if self.state["stage"] == "accept":
            self.accept()
        return self.finish("done")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--model", default="opus", help="strong model: audit, acceptance, escalation")
    ap.add_argument("--writer", default="sonnet", help="model that writes acceptance tests and code")
    ap.add_argument("--max-hours", type=float, default=8)
    ap.add_argument("--max-usd", type=float, default=60, help="cumulative notional budget across all sessions")
    ap.add_argument("--gate-timeout", type=int, default=1800)
    ap.add_argument("--permission-mode", default="bypassPermissions")
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--stream", action="store_true")
    ap.add_argument("--no-wait", action="store_true")
    a = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    return Run(a).run()


if __name__ == "__main__":
    sys.exit(main())
