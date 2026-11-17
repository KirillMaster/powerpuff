"""forge v2 - script-orchestrated spec -> program run. The orchestrator is this script, not a model.

Stages (state in .forge/state.json, resumable; every stage is one `claude -p` session):
  audit   (strong model)  spec -> .forge/requirements.md (R/D lines), digest.md, gates.json
  tests   (writer model)  acceptance tests from R/D, context never sees an implementation; frozen by sha256;
                          then the test gates must be red on the tree without implementation (else one `vacuous` session)
  build  (writer model)  ONE session implements everything; forge_check decides; red -> resume same session (<=3),
                          then one strong-model escalation
  accept  (strong model)  ONE fresh pass over diff vs spec -> .forge/gaps.json; gaps -> writer fix round (<=2)
  report  (script)        .forge/REPORT.md, last stdout line `FORGE: done|failed`

Usage: python forge_run.py <spec-dir|spec-file|ticket text> [--repo .] [--model opus] [--writer sonnet]
       [--max-hours 8] [--max-usd 60] [--preflight] [--stream] [--no-wait] [--allow-red-base] [--assume-defaults]
  --stream   re-emit the sessions' stream-json on stdout and finish with one synthetic `result` event (bob-bench)
  --no-wait  on a rate limit emit the result and exit 3 instead of sleeping (the caller pauses and re-invokes)
  --allow-red-base   continue when gates are red on the untouched base tree (default: failed, no session is spent)
  --assume-defaults  use the auditor's defaults for unanswered `blocking` questions (.forge/questions.json)
  --final    after green acceptance run the profile tier `final` once (red: `final gates red: <names>`, resumable);
             without it tier `final` is left to the end of a queue run (queue_run.py)
Gate commands may use {base} (start_sha of the run), {root} and {plugin} (plugin root); also exported as
POWERPUFF_BASE / POWERPUFF_ROOT / POWERPUFF_PLUGIN. The report stage also writes .forge/quality-report.json (schema 1).
Detached on Windows: start "" /b cmd /c "set PYTHONUNBUFFERED=1&& python forge_run.py specs/001 > forge.log 2>&1"
"""

import argparse
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_check  # noqa: E402
import gate_guard  # noqa: E402

PROFILE_FILES = ("powerpuff.yaml", "powerpuff.gates.json")
DRAFT_NOTE = "NOT APPROVED (chosen by the model; review .forge/powerpuff.draft.yaml and save it as powerpuff.yaml)"
RATE = ("usage limit", "rate limit", "429", "overloaded")
EPOCH = re.compile(r"\|\s*(\d{10})\b")
CLOCK = re.compile(r"\bresets?(?:\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)\b", re.IGNORECASE)
SPEC_FILES = ("spec.yaml", "spec.md", "plan.yaml", "plan.md", "tasks.yaml", "tasks.md", "contracts.yaml",
              "data-model.yaml")
TEST_INFRA = ("*.csproj", "*.sln", "*.slnx", "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
              "pyproject.toml", "requirements*.txt", "Cargo.toml", "Cargo.lock", "go.mod", "go.sum", "conftest.py")
Q_LINE = re.compile(r"^\s*[-*]?\s*\**Q(\d+)\b")
Q_FULL = re.compile(r"^\s*[-*]?\s*\**Q(\d+)\**\s*\[(blocking|non-blocking|info)\]\s*:\s*(.+?)\s*\|\s*default:\s*(.+?)"
                    r"\s*\|\s*cost of a wrong guess:\s*(.+?)\s*$", re.IGNORECASE)
Q_CLASSES = ("blocking", "non-blocking", "info")

AUDIT_HEAD = """You are the spec auditor of an unattended build. Do NOT write production code or tests.
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
   - `Q<n> [blocking|non-blocking|info]: <question> | default: <the D-line id or the chosen default> | cost of a wrong
     guess: <one phrase>` - one line per question, exactly this form. `blocking` ONLY when a wrong guess would make
     the whole unit worthless or irreversible (data loss, external side effects, public API shape); everything else
     gets a default (also as a D line) and is `non-blocking`; `info` = a noteworthy assumption that needs no answer.
2. `.forge/digest.md` (<= 80 lines): stack, how to build/test, module map (path -> responsibility), conventions with
   path:line (error format, naming, test layout, fixtures), files this spec will touch.
"""
AUDIT_GATES = """3. `.forge/gates.json`: {"format_fix": "<command or null>", "gates": {"<name>": "<command>"}} - the format/lint/
   architecture/test commands that must exit 0 at the end, run from the repo root. Take them from
   `.claude/bob-pipeline.yaml` (tests.run_command, enabled tools of category lint/format/architecture) if present,
   else from the manifests (dotnet build -warnaserror / dotnet format --verify-no-changes / dotnet test; package.json
   lint/test scripts or npx tsc --noEmit; ruff check . / ruff format --check . / pytest -q). Only commands that work
   in this repo (install dependencies if needed and check the tools exist). There must be a test gate.
"""
AUDIT_PROFILE = ("Do NOT write `.forge/gates.json`: the gate commands come from the project profile `powerpuff.yaml` "
                 "and are not yours to choose.\n")
AUDIT_TAIL = "Finish with one line: AUDIT: done"
AUDIT = AUDIT_HEAD + AUDIT_GATES + AUDIT_TAIL
AUDIT_WITH_PROFILE = AUDIT_HEAD.replace("exactly three files", "exactly two files") + AUDIT_PROFILE + AUDIT_TAIL

TESTS = """You write the acceptance tests of an unattended build. Do NOT write production code.
Inputs: `.forge/spec.md`, `.forge/requirements.md` (R and D lines are binding), `.forge/digest.md`.
Write executable acceptance tests through the public interface (HTTP/CLI/public API) in the repo's test layout, in
new files whose names contain `acceptance`. Every R has at least one test; the test name or a comment contains its
id (`R7`). Assert exact values from the spec (statuses, numbers, order of checks, rounding). Black-box only: use the
documented entry points; when the implementation does not exist yet the tests must fail for that reason only (they
must be syntactically valid and collectable where the framework allows it).
Then write `.forge/acceptance-files.txt`: one repo-relative path per line, every acceptance test file.
Finish with one line: TESTS: done"""

TOPUP = """You complete the acceptance tests of an unattended build. Do NOT write or change production code and do NOT
edit existing acceptance test files. These requirements from `.forge/requirements.md` have no acceptance test yet:
UNCOVERED
Write tests for them in NEW files whose names contain `acceptance` (same rules: public interface, exact values from
`.forge/spec.md`, the test name or a comment contains the requirement id). Append the new paths to
`.forge/acceptance-files.txt`. Finish with one line: TESTS: done"""

VACUOUS = """The acceptance tests pass without any implementation, so they do not assert the new behaviour. Do NOT
write or change production code and do NOT edit existing acceptance test files.
Read `.forge/spec.md` and `.forge/requirements.md` and add acceptance tests in NEW files whose names contain
`acceptance` that FAIL until the spec is implemented (public interface, exact values from the spec, the test name or a
comment contains the requirement id). Append the new paths to `.forge/acceptance-files.txt`.
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
Guard violations (`guard` in the output) must be removed, not worked around; a suppression that is truly required
goes to `.forge/disputes.md` with the reason.

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


def sha(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def yq(s: str) -> str:
    return f"'{s}'" if "'" not in s else json.dumps(s, ensure_ascii=False)


def parse_question(line: str) -> dict:
    """One `Q<n>` line of requirements.md -> questions.json entry; anything off the strict form is kept verbatim as
    a non-blocking, `unparsed` question."""
    m = Q_FULL.match(line)
    if m:
        return {"id": f"Q{m.group(1)}", "class": m.group(2).lower(), "question": m.group(3), "default": m.group(4),
                "wrong_guess_cost": m.group(5), "answer": None}
    return {"id": f"Q{Q_LINE.match(line).group(1)}", "class": "non-blocking", "question": line.strip(),  # type: ignore[union-attr]
            "default": "", "wrong_guess_cost": "", "answer": None, "unparsed": True}


def answered(q: dict) -> bool:
    return q.get("answer") is not None and bool(str(q["answer"]).strip())


def question_text(q: dict) -> str:
    return re.sub(rf"^\W*{re.escape(str(q.get('id')))}\W*", "", str(q.get("question", "")))


def profile_snapshot(root: Path) -> dict[str, bytes | None]:
    return {n: (root / n).read_bytes() if (root / n).is_file() else None for n in PROFILE_FILES}


def load_profile(root: Path) -> dict | None:
    """The human-owned gate profile (powerpuff.yaml / powerpuff.gates.json) or None; ValueError = configuration error."""
    if not any((root / n).is_file() for n in PROFILE_FILES):
        return None
    try:
        cfg = gate_guard.load_config(root, None)
    except Exception as e:  # noqa: BLE001 - bad JSON/YAML in the profile is a configuration error, not a crash
        raise ValueError(f"cannot read profile: {e}") from None
    tiers, cmds = cfg.get("gates"), cfg.get("commands")
    if not isinstance(tiers, dict) or not isinstance(cmds, dict):
        raise ValueError("profile needs `gates` (tier -> list of gate ids) and `commands` (gate id -> command)")
    lists: dict[str, list[str]] = {}
    for tier, ids in tiers.items():
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ValueError(f"profile gates.{tier} must be a list of gate ids")
        for i in ids:
            if not isinstance(cmds.get(i), str) or not cmds[i].strip():
                raise ValueError(f"gate '{i}' is listed in gates.{tier} but has no command in `commands`")
        lists[tier] = ids
    ids = list(dict.fromkeys(lists.get("iteration", []) + lists.get("unit", [])))
    if not ids:
        raise ValueError("profile lists no gates in `iteration` or `unit`")
    test_gates = cfg.get("test_gates")
    if test_gates is not None and (not isinstance(test_gates, list) or not all(i in ids for i in test_gates)):
        raise ValueError("profile test_gates must be a list of gate ids from `iteration` or `unit`")
    approved = cfg.get("approved") is True or str(cfg.get("approved")).strip().lower() == "true"
    return {"approved": approved, "gates": {i: cmds[i] for i in ids}, "final": lists.get("final", []),
            "final_gates": {i: cmds[i] for i in lists.get("final", [])},
            "format_fix": str(cfg["format_fix"]) if cfg.get("format_fix") else None, "config": cfg}


def failure_brief(rep: dict, limit: int = 8000) -> str:
    """The check report without green noise, so the red parts are never cut off: same shape, but only failed
    gates keep their output and green sections shrink to {"ok": true}."""
    out: dict = {"done": bool(rep.get("done"))}
    if rep.get("gates_tampered"):
        out["gates_tampered"] = True
    for key in ("frozen", "coverage", "guard"):
        part = rep.get(key)
        if isinstance(part, dict):
            out[key] = part if not part.get("ok", True) else {"ok": True}
    gates = rep.get("gates") or {}
    out["gates"] = {"ok": bool(gates.get("ok")),
                    "results": {k: v for k, v in (gates.get("results") or {}).items() if not v.get("ok")}}
    return json.dumps(out, ensure_ascii=False)[:limit]


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
        self.profile: dict | None = None  # raw profile config, the guard's config
        self.snapshot: dict[str, bytes | None] = {}  # profile files as they were when the gates were fixed

    # ---- plumbing ---------------------------------------------------------------------------------
    def log(self, msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        with open(self.forge / "progress.md", "a", encoding="utf-8") as f:
            f.write("- " + line + "\n")
        print(line, file=sys.stderr if self.a.stream else sys.stdout, flush=True)

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state, indent=1, ensure_ascii=False), encoding="utf-8")

    def git(self, *args: str) -> str:
        p = subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=self.root, capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        return p.stdout.strip()

    def commit(self, msg: str) -> None:
        self.git("add", "-A", "--", ".", ":!.forge")
        self.git("-c", "user.name=forge", "-c", "user.email=forge@local", "commit", "-q", "--no-gpg-sign", "-m", msg)

    def commit_frozen(self, msg: str) -> None:
        self.commit(msg)
        self.state["frozen_sha"] = self.git("rev-parse", "HEAD")
        self.save()

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
                                shell=sys.platform == "win32", env={**os.environ, "POWERPUFF_DRIVER": "1"})
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
                 + ("" if ok else " error=" + " ".join(text.split())[:200] + " tail=" + " | ".join(tail[-3:])[:300]))
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
        src = Path(self.a.spec) if self.a.spec.strip() and len(self.a.spec) < 260 and "\n" not in self.a.spec else None
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

    def fix_gates(self, gates: dict, fmt: str | None, source: str) -> None:
        """From here on the gates live in the driver (state in memory); the files on disk are only compared."""
        self.state.update(gates=gates, format_fix=fmt, gates_source=source, gates_sha256=sha(gates))
        self.snapshot = profile_snapshot(self.root) if source == "profile" else {}
        self.save()

    def adopt_profile(self) -> bool:
        try:
            prof = load_profile(self.root)
        except ValueError as e:
            raise SystemExit(self.finish("failed", f"profile: {e}")) from None
        if not prof:
            return False
        self.profile = prof["config"]
        self.state.update(final_gates=prof["final"], final_commands=prof["final_gates"],
                          profile_approved=prof["approved"])
        self.fix_gates(prof["gates"], prof["format_fix"], "profile")
        return True

    def write_draft(self) -> None:
        """Ready-to-approve profile from the gates the model drafted (iteration tier = all of them)."""
        gates: dict = self.state["gates"]
        key = lambda g: g if re.fullmatch(r"[\w-]+", g) else yq(g)  # noqa: E731
        lines = ["approved: false", "gates:", f"  iteration: [{', '.join(yq(g) for g in gates)}]", "commands:",
                 *(f"  {key(g)}: {yq(c)}" for g, c in gates.items())]
        if self.state.get("format_fix"):
            lines.append(f"format_fix: {yq(self.state['format_fix'])}")
        (self.forge / "powerpuff.draft.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def resume_gates(self) -> None:
        """After a restart the gates come from the profile again, or must match the hash stored at the audit."""
        if self.state.get("gates_source") == "profile":
            self.adopt_profile()
            return
        gates = self.state.get("gates")
        if not isinstance(gates, dict) or not gates:
            return
        cfg = self.gates_cfg()
        want = self.state.get("gates_sha256") or sha(gates)
        if sha(cfg.get("gates")) != want or sha(gates) != want:
            self.state["gates"] = {}
            raise SystemExit(self.finish("failed", "gates changed since the audit (.forge/gates.json hash mismatch)"))
        self.state.setdefault("format_fix", cfg.get("format_fix") or None)
        self.fix_gates(gates, self.state["format_fix"], "model-draft")

    def restore_gate_config(self) -> bool:
        """Undoes edits of the gate sources made after the gates were fixed; True when something was changed."""
        bad = False
        if self.state.get("gates_source") == "profile":
            for name, was in self.snapshot.items():
                p = self.root / name
                if (p.read_bytes() if p.is_file() else None) != was:
                    bad = True
                    p.write_bytes(was) if was is not None else p.unlink(missing_ok=True)
        elif self.state.get("gates_source") == "model-draft" \
                and sha(self.gates_cfg().get("gates")) != self.state.get("gates_sha256"):
            bad = True
            (self.forge / "gates.json").write_text(
                json.dumps({"format_fix": self.state.get("format_fix"), "gates": self.state["gates"]}), encoding="utf-8")
        if bad:
            self.state["gates_tampered"] = True
            self.log("check: gate configuration was edited by a session; restored")
            self.commit("forge: restore gate configuration")
        return bad

    def fail_resumable(self, why: str, stage: str) -> int:
        """A failure the human can fix: the report is written, but the next run continues at `stage`."""
        rc = self.finish("failed", why)
        self.state["stage"] = stage
        self.save()
        return rc

    def gate_ctx(self, head: bool = False) -> dict[str, str]:
        """{base} = the commit the run started from (state start_sha, set at init); in the base check = current HEAD."""
        base = self.git("rev-parse", "HEAD") if head else self.state.get("start_sha") or self.git("rev-parse", "HEAD")
        return forge_check.gate_context(self.root, base)

    def gate_tier(self, name: str) -> str:
        lists = (self.profile or {}).get("gates") or {}
        return next((t for t in ("final", "unit") if name in (lists.get(t) or [])), "iteration")

    def note_gates(self, res: dict) -> dict:
        """Remembers the last result of every gate that ran (state `gate_last`) for .forge/quality-report.json."""
        last = self.state.setdefault("gate_last", {})
        for name, r in res.items():
            last[name] = forge_check.gate_row(name, self.gate_tier(name), r)
        return res

    def write_quality_report(self, guard: dict | None) -> None:
        rows = list((self.state.get("gate_last") or {}).values())
        if not rows:
            return
        rep = forge_check.quality_report("powerpuff.forge_run", self.git("rev-parse", "HEAD"), self.state.get("start_sha"),
                                         bool(self.state.get("profile_approved")), "all", rows,
                                         **({"guard": guard} if guard else {}))
        (self.forge / "quality-report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False) + "\n",
                                                         encoding="utf-8")

    def base_check(self, stage: str) -> None:
        """All active gates once on the untouched base tree, before anything is spent on it."""
        if "base_checked" in self.state:
            return
        if self.a.skip_base_check:
            self.state["base_checked"] = "skipped"
            self.save()
            return
        gates: dict = self.state.get("gates") or {}
        res = self.note_gates(forge_check.check_gates(self.root, self.a.gate_timeout, gates,
                                                      self.gate_ctx(head=True))["results"]) if gates else {}
        for name, r in res.items():
            if r.get("missing_tool"):
                words = gates[name].split()
                self.state["base_red_tails"] = {name: r.get("tail", "")}
                raise SystemExit(self.fail_resumable(
                    f"base check: missing tool '{words[0] if words else gates[name]}' (gate '{name}')", stage))
        red = {n: r.get("tail", "") for n, r in res.items() if not r["ok"]}
        if red:
            self.state["base_red_tails"] = red
            if not self.a.allow_red_base:
                raise SystemExit(self.fail_resumable(f"base is red: {', '.join(red)}", stage))
            self.state["base_red"] = list(red)
            self.log(f"WARNING: base is red ({', '.join(red)}), continuing (--allow-red-base); still required at the end")
        self.state["base_checked"] = True
        self.save()

    def load_questions(self) -> list[dict] | None:
        p = self.forge / "questions.json"
        if not p.is_file():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, list) and all(isinstance(q, dict) for q in data) else None

    def write_questions(self) -> None:
        """requirements.md Q lines -> .forge/questions.json; answers already given for the same question are kept."""
        req_path = self.forge / "requirements.md"
        lines = [x for x in req_path.read_text(encoding="utf-8").splitlines() if Q_LINE.match(x)] \
            if req_path.is_file() else []
        old = {(q.get("id"), q.get("question")): q.get("answer") for q in self.load_questions() or []}
        qs = [parse_question(x) for x in lines]
        for q in qs:
            q["answer"] = old.get((q["id"], q["question"]))
        (self.forge / "questions.json").write_text(json.dumps(qs, indent=1, ensure_ascii=False), encoding="utf-8")

    def question_counts(self) -> str:
        qs = self.load_questions() or []
        return " ".join(f"{c}={sum(1 for q in qs if q.get('class') == c)}" for c in Q_CLASSES)

    def questions_gate(self) -> None:
        """A blocking question without an answer is never guessed, unless --assume-defaults says so."""
        if self.state.get("questions_ok"):
            return
        if not (self.forge / "questions.json").is_file():
            self.write_questions()
        qs = self.load_questions()
        if qs is None:
            raise SystemExit(self.fail_resumable("questions.json is not a JSON list of questions", "tests"))
        open_q = [q for q in qs if q.get("class") == "blocking" and not answered(q)]
        if open_q and not self.a.assume_defaults:
            self.log("answer the blocking questions in .forge/questions.json (`answer`) and rerun, or pass --assume-defaults")
            raise SystemExit(self.fail_resumable(f"blocking questions: {','.join(str(q.get('id')) for q in open_q)}",
                                                 "tests"))
        if open_q:
            self.state["assumed_defaults"] = [str(q.get("id")) for q in open_q]
            self.log(f"questions: assuming defaults for {', '.join(self.state['assumed_defaults'])}")
        self.state["questions_ok"] = True
        self.save()

    def human_decisions(self) -> str:
        qs = [q for q in self.load_questions() or [] if answered(q)]
        if not qs:
            return ""
        return "\n\nHuman decisions:\n" + "\n".join(
            f"- {q.get('id')}: {question_text(q)} => {str(q['answer']).strip()} (binding)" for q in qs)

    def changed_paths(self) -> tuple[list[str], list[str]]:
        """(tracked files that differ from HEAD, untracked files), `.forge/` excluded."""
        def z(*args: str) -> list[str]:
            return [x for x in self.git("-c", "core.quotepath=off", *args).split("\0") if x and not x.startswith(".forge/")]
        return z("diff", "--name-only", "--no-renames", "-z", "HEAD"), z("ls-files", "--others", "--exclude-standard", "-z")

    def dirty_snapshot(self) -> dict[str, dict]:
        """What is already dirty before a test-writing session: that is the human's, not the session's."""
        tracked, untracked = self.changed_paths()
        read = lambda f: (self.root / f).read_bytes() if (self.root / f).is_file() else None  # noqa: E731
        return {"tracked": {f: read(f) for f in tracked},
                "untracked": {f: hashlib.sha256(read(f) or b"").hexdigest() for f in untracked}}

    def keep_additive(self, stage: str, before: dict[str, dict]) -> None:
        """A test-writing session may only add files: tracked files it touched are restored (manifests of the test
        infrastructure excepted), new files stay only when they are acceptance tests or test support. Files that
        were already dirty before the session (`before`) go back to their pre-session state, never to HEAD."""
        extra = (self.profile or {}).get("test_infra")
        globs = [*TEST_INFRA, *(str(x) for x in extra if isinstance(x, str))] if isinstance(extra, list)             else list(TEST_INFRA)
        infra = lambda p: any(fnmatch.fnmatch(posixpath.basename(p), g) or fnmatch.fnmatch(p, g) for g in globs)  # noqa: E731
        try:
            by_ext = gate_guard.build_languages(self.profile or {})
        except gate_guard.GuardError:
            by_ext = gate_guard.build_languages({})
        listed = self.acceptance_files()
        dirs = {posixpath.dirname(f) for f in listed} - {""}
        self.git("reset", "-q")
        tracked, untracked = self.changed_paths()
        gone: list[str] = []
        for f in tracked:
            if infra(f):
                continue
            path = self.root / f
            if f in before["tracked"]:
                was = before["tracked"][f]
                if (path.read_bytes() if path.is_file() else None) == was:
                    continue
                path.write_bytes(was) if was is not None else path.unlink(missing_ok=True)
            else:
                self.git("checkout", "HEAD", "--", f)
            gone.append(f)
            self.log(f"{stage}: reverted {f}")
        for f in untracked:
            if f in before["untracked"] or f in listed or infra(f) or posixpath.dirname(f) in dirs                     or gate_guard.is_test_file(f, by_ext.get(gate_guard.ext(f))):
                continue
            (self.root / f).unlink(missing_ok=True)
            gone.append(f)
            self.log(f"{stage}: removed {f}")
        if gone:
            self.state["tests_stage_reverted"] = list(dict.fromkeys([*self.state.get("tests_stage_reverted", []), *gone]))
            self.save()

    def audit(self) -> None:
        profile = self.adopt_profile()
        if profile:
            self.base_check("audit")

        def done() -> bool:
            req = self.forge / "requirements.md"
            gates = self.gates_cfg().get("gates")
            return req.is_file() and bool(forge_check.R_LINE.search(req.read_text(encoding="utf-8"))) \
                and (profile or isinstance(gates, dict) and bool(gates))
        self.staged("audit", self.a.model, AUDIT_WITH_PROFILE if profile else AUDIT, done)
        if not done():
            raise SystemExit(self.finish("failed", "audit produced no requirements/gates"))
        if not profile:
            cfg = self.gates_cfg()
            self.fix_gates(cfg["gates"], cfg.get("format_fix") or None, "model-draft")
            self.write_draft()
        self.write_questions()
        self.state["stage"] = "tests"
        self.save()

    def acceptance_files(self) -> list[str]:
        listing = self.forge / "acceptance-files.txt"
        if not listing.is_file():
            return []
        seen = dict.fromkeys(x.strip().replace(chr(92), "/") for x in listing.read_text(encoding="utf-8").splitlines())
        return [x for x in seen if x and (self.root / x).is_file()]

    def uncovered(self) -> list[str]:
        return forge_check.check_coverage(self.root, dict.fromkeys(self.acceptance_files(), ""))["uncovered"]

    def ensure_coverage(self, tries: int) -> None:
        """Every R must be referenced by a frozen acceptance test BEFORE the build; missing ones get a top-up session
        that may only add new test files (already frozen files are restored if touched)."""
        for i in range(tries):
            missing = self.uncovered()
            if not missing:
                break
            self.log(f"tests: uncovered {missing}, top-up {i + 1}")
            before = self.dirty_snapshot()
            self.staged(f"tests-topup{i + 1}", self.state["writer"],
                        TOPUP.replace("UNCOVERED", ", ".join(missing)) + self.human_decisions(),
                        lambda: not self.uncovered())
            self.keep_additive(f"tests-topup{i + 1}", before)
            frozen = forge_check.frozen_files(self.root)
            for f in forge_check.check_frozen(self.root, frozen)["changed_or_missing"]:
                self.git("checkout", "HEAD", "--", f)
        self.freeze(self.acceptance_files())
        self.commit_frozen("forge: acceptance tests")

    def tests(self) -> None:
        before = self.dirty_snapshot()
        self.staged("tests", self.state["writer"], TESTS + self.human_decisions(), lambda: bool(self.acceptance_files()))
        self.keep_additive("tests", before)
        if not self.acceptance_files():
            raise SystemExit(self.finish("failed", "no acceptance tests written"))
        self.freeze(self.acceptance_files())
        self.commit_frozen("forge: acceptance tests")
        self.ensure_coverage(2)
        if "red_on_base" not in self.state:
            self.red_on_base()
        self.state["stage"] = "build"
        self.save()

    def test_gates(self) -> dict[str, str]:
        gates: dict = self.state.get("gates") or {}
        chosen = (self.profile or {}).get("test_gates") if self.state.get("gates_source") == "profile" else None
        if isinstance(chosen, list) and chosen:
            return {g: gates[g] for g in chosen if g in gates}
        by_id = self.state.get("gates_source") == "profile"
        return {g: c for g, c in gates.items() if "test" in g.lower() or by_id and g.lower() == "acceptance"}

    def tests_red(self, gates: dict[str, str]) -> bool:
        """True when at least one test gate fails on the tree without implementation; a missing tool is a failure."""
        res = self.note_gates(forge_check.check_gates(self.root, self.a.gate_timeout, gates, self.gate_ctx())["results"])
        for name, r in res.items():
            if r.get("missing_tool"):
                raise SystemExit(self.finish("failed", f"red on base: test gate '{name}' cannot run (missing tool)"))
        return any(not r["ok"] for r in res.values())

    def red_on_base(self) -> None:
        """Frozen tests that already pass without an implementation prove nothing: run the test gates once now."""
        gates = self.test_gates()
        if not gates:
            self.state["red_on_base"] = "no test gate"
            self.log("tests: no test gate, red-on-base check skipped")
            self.save()
            return
        red = self.tests_red(gates)
        if not red:
            self.log("tests: acceptance tests pass without an implementation, vacuous session")
            known = set(self.acceptance_files())
            before = self.dirty_snapshot()
            self.staged("tests-vacuous", self.state["writer"], VACUOUS + self.human_decisions(),
                        lambda: bool(set(self.acceptance_files()) - known))
            self.keep_additive("tests-vacuous", before)
            frozen = forge_check.frozen_files(self.root)
            for f in forge_check.check_frozen(self.root, frozen)["changed_or_missing"]:
                self.git("checkout", "HEAD", "--", f)
            self.freeze(self.acceptance_files())
            self.commit_frozen("forge: acceptance tests (vacuous round)")
            red = self.tests_red(gates)
        self.state["red_on_base"] = red
        self.log(f"tests: red on base = {red}")
        self.save()

    def freeze(self, files: list[str]) -> None:
        lines = [f"{hashlib.sha256((self.root / f).read_bytes()).hexdigest()}  {f}" for f in files]
        (self.forge / "frozen.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def guard(self) -> dict:
        """Anti-tampering diff check against the commit where the acceptance tests were frozen."""
        base = self.state.get("frozen_sha") or self.state.get("start_sha") or "HEAD"
        try:
            g = gate_guard.check(self.root, base, self.profile)
        except gate_guard.GuardError as e:
            return {"ok": False, "error": str(e), "violations": [], "counts": {}, "tests": {}, "unknown_extensions": []}
        return {"ok": g["ok"], "violations": g["violations"][:20], "counts": g["counts"], "tests": g["tests"],
                "unknown_extensions": g["unknown_extensions"]}

    def check(self) -> dict:
        gates_tampered = self.restore_gate_config()
        files = forge_check.frozen_files(self.root)
        tampered = forge_check.check_frozen(self.root, files)["changed_or_missing"]
        for f in tampered:  # a tampered frozen test is restored before the gates run, never accepted
            self.git("checkout", self.state.get("frozen_sha", "HEAD"), "--", f)
        self.save()  # the checker sees the driver's state, not what a session left in state.json
        chk = forge_check.check_gates(self.root, self.a.gate_timeout, self.state.get("gates") or {}, self.gate_ctx())
        self.note_gates(chk["results"])
        rep: dict = {"coverage": forge_check.check_coverage(self.root, files),
                     "frozen": forge_check.check_frozen(self.root, files), "gates": chk, "guard": self.guard()}
        if tampered:
            rep["frozen"] = {"ok": False, "files": len(files), "changed_or_missing": tampered,
                             "note": "restored from git; frozen acceptance tests must not be edited"}
        if gates_tampered:
            rep["gates_tampered"] = True
        rep["done"] = all(rep[k]["ok"] for k in ("coverage", "frozen", "gates", "guard")) and not gates_tampered
        (self.forge / "check.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
        gates = {k: v["ok"] for k, v in rep["gates"].get("results", {}).items()}
        self.log(f"check: done={rep['done']} coverage={rep['coverage']['ok']} frozen={rep['frozen']['ok']} "
                 f"gates={gates} guard={rep['guard']['ok']}")
        return rep

    def build_prompt(self) -> str:
        ctx = self.gate_ctx()
        gates = "\n".join(f"  - {n}: `{forge_check.expand(c, ctx)}`" for n, c in (self.state.get("gates") or {}).items())
        fmt = forge_check.expand(str(self.state.get("format_fix") or "none"), ctx)
        return BUILD.replace("FORMAT_FIX", fmt).replace("GATES", gates) \
            + self.human_decisions()

    def build_loop(self, stage: str, prompt: str, rounds: int) -> bool:
        if self.uncovered():  # e.g. a run resumed from an older state
            self.ensure_coverage(1)
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
            prompt = FIX + failure_brief(rep)
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
                self.commit_frozen("forge: acceptance tests corrected by reviewer")
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

    def final_tier(self) -> None:
        """--final: profile tier `final` once after acceptance is green; red = failed, the next run repeats only this."""
        cmds: dict = self.state.get("final_commands") or {}
        if not cmds or self.state.get("final_ok"):
            return
        res = self.note_gates(forge_check.check_gates(self.root, self.a.gate_timeout, cmds, self.gate_ctx())["results"])
        self.state["final_results"] = {n: r["ok"] for n, r in res.items()}
        red = [n for n, r in res.items() if not r["ok"]]
        self.log(f"final: {self.state['final_results']}")
        if red:
            self.state["final_red_tails"] = {n: res[n].get("tail", "") for n in red}
            raise SystemExit(self.fail_resumable(f"final gates red: {', '.join(red)}", "report"))
        self.state["final_ok"] = True
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
        guard = rep.get("guard") or {}
        trust = {"profile": "approved profile" if self.state.get("profile_approved") else "profile not approved",
                 "model-draft": DRAFT_NOTE}.get(self.state.get("gates_source", ""), "")
        gates = ", ".join(f"{k}={'pass' if v['ok'] else 'FAIL'}"
                          for k, v in (rep.get("gates", {}).get("results") or {}).items())
        red = self.state.get("red_on_base")
        red_line = "unknown" if red is None else red if isinstance(red, str) else "yes" if red else "no (vacuous)"
        qs = self.load_questions() or []
        q_lines: list[str] = []
        for cls in Q_CLASSES:
            items = [q for q in qs if q.get("class") == cls]
            if items:
                q_lines += [f"### {cls}", *(
                    (f"- {q.get('question')} (unparsed)" if q.get("unparsed") else
                     f"- {q.get('id')}: {q.get('question')} | default: {q.get('default')} | cost of a wrong guess: "
                     f"{q.get('wrong_guess_cost')}") + f" | answer: {q['answer'] if answered(q) else '(none)'}"
                    for q in items)]
        q_lines = q_lines or pick("Q")
        assumed = self.state.get("assumed_defaults") or []
        n_gone = len(self.state.get("tests_stage_reverted") or [])
        base_red = self.state.get("base_red") or []
        tails = self.state.get("base_red_tails") or {}
        self.write_quality_report(rep.get("guard"))
        last = list((self.state.get("gate_last") or {}).values())
        q_table = ["- quality report: .forge/quality-report.json", "", "| gate | result | seconds |", "|---|---|---|",
                   *(f"| {r['name']} | {'pass' if r['ok'] else 'MISSING TOOL' if r['missing_tool'] else 'FAIL'} | "
                     f"{r['seconds']:.1f} |" for r in last), ""] if last else []
        lines = [f"# FORGE REPORT - {status.upper()}" + (f" ({why})" if why else ""), "",
                 *([f"- test-writer changes reverted: {n_gone} file(s)"] if n_gone else []),
                 *([f"- base was red (allowed by --allow-red-base): {', '.join(base_red)}"] if base_red else []),
                 f"- requirements: {len(pick('R'))}, uncovered: {rep.get('coverage', {}).get('uncovered', 'n/a')}",
                 f"- frozen tests intact: {rep.get('frozen', {}).get('ok', 'n/a')}",
                 f"- acceptance tests fail before implementation: {red_line}",
                 *(["- acceptance tests: PASS WITHOUT IMPLEMENTATION (vacuous) - review"]
                   if self.state.get("red_on_base") is False else []),
                 f"- gate results: {gates or 'n/a'}", *q_table,
                 *([f"- final gates: {self.state['final_results']}"] if self.state.get("final_results") else []),
                 *([f"- gates: {trust}"] if trust else []),
                 *(["- gates were edited during the run and restored (gates_tampered)"]
                   if self.state.get("gates_tampered") else []),
                 f"- guard: ok={guard.get('ok', 'n/a')} counts={guard.get('counts', 'n/a')} "
                 f"tests={guard.get('tests', 'n/a')}",
                 *([f"- guard is blind for: {', '.join(guard['unknown_extensions'])}"]
                   if guard.get("unknown_extensions") else []),
                 f"- cost (notional USD): {c['total_usd']} by model {c['by_model']} by stage {c['by_stage']}; "
                 f"sessions {c['sessions']}", "",
                 *(["## Base check (output tails of red gates)",
                    *(f"- {n}: {' '.join(str(t).split())[-600:]}" for n, t in tails.items()), ""] if tails else []),
                 "## Decisions where the spec was silent (review these)", *(pick("D") or ["none"]), "",
                 *(["## Assumed defaults (blocking questions without a human answer, --assume-defaults)",
                    *(f"- {q.get('id')}: {question_text(q)} -> default: {q.get('default')}"
                      for q in qs if q.get("id") in assumed and not answered(q)), ""] if assumed else []),
                 "## Questions for the human", *((q_lines + (self.state.get("questions") or [])) or ["none"]), "",
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
        if self.state["stage"] != "audit":
            self.resume_gates()
        if self.state["stage"] == "audit":
            self.audit()
            if self.a.preflight:
                self.log(f"preflight: questions {self.question_counts()} -> {self.forge / 'questions.json'}")
                self.log("preflight: answer blocking Q lines in questions.json (`answer`), then rerun")
                print("FORGE: preflight", flush=True)
                return 0
        if self.state["stage"] == "tests":
            self.questions_gate()
            self.base_check("tests")
            self.tests()
        if self.state["stage"] == "build":
            self.build()
        if self.state["stage"] == "accept":
            self.accept()
        if self.a.final and not (self.state.get("open_gaps") or not self.state.get("build_green", True)):
            self.final_tier()
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
    ap.add_argument("--allow-red-base", action="store_true", help="continue when gates are red on the untouched base")
    ap.add_argument("--assume-defaults", action="store_true", help="use defaults for unanswered blocking questions")
    ap.add_argument("--final", action="store_true", help="also run the profile tier `final` once after acceptance")
    ap.add_argument("--skip-base-check", action="store_true", help=argparse.SUPPRESS)
    a =ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    return Run(a).run()


if __name__ == "__main__":
    sys.exit(main())
