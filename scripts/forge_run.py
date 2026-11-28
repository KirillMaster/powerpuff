"""forge v2 - script-orchestrated spec -> program run. The orchestrator is this script, not a model.

Engines (`--engine`, state key `engine`): `plain` (default) = ONE model context continued with `--resume`:
  plain-tests  turn 1: questions + acceptance tests as NEW files only; script guard, freeze (.forge/frozen.sha256,
               .forge/frozen.json for hook_protect), red check on the base
  plain-build  turn 2: implement; script gates (profile required), red -> resume the SAME session with the failing
               gates, <= --max-fix-rounds; `--accept` adds the acceptance pass below at the end
`forge` = the multi-session pipeline below, unchanged.

Stages of the forge engine (state in .forge/state.json, resumable; every stage is one `claude -p` session):
  audit   (strong model)  spec -> .forge/requirements.md (R/D lines), digest.md, gates.json
  tests   (writer model)  acceptance tests from R/D, context never sees an implementation; frozen by sha256;
                          then the test gates must be red on the tree without implementation (else one `vacuous` session)
  build  (writer model)  ONE session implements everything; forge_check decides; red -> resume same session (<=3),
                          then one strong-model escalation
  accept  (strong model)  ONE fresh pass over diff vs spec -> .forge/gaps.json; gaps -> writer fix round (<=2)
  report  (script)        .forge/REPORT.md, last stdout line `FORGE: done|failed`

Usage: python forge_run.py <spec-dir|spec-file|ticket text> [--repo .] [--engine plain|forge] [--model opus]
       [--writer sonnet] [--max-fix-rounds 4] [--accept] [--max-hours 8] [--max-usd 60] [--preflight] [--stream] [--no-wait] [--allow-red-base] [--assume-defaults]
  --stream   re-emit the sessions' stream-json on stdout and finish with one synthetic `result` event (bob-bench)
  --no-wait  on a rate limit emit the result and exit 3 instead of sleeping (the caller pauses and re-invokes)
  --max-outage-min  API unreachable (network/5xx): wait 30 s, 60 s, ... <=15 min per step without spending a try;
             after N minutes in total (or with --no-wait at once) `FORGE: paused (API unreachable)`, exit 3, resumable
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
RATE = ("usage limit", "rate limit", "429", "overloaded", "hit your", "5-hour limit", "weekly limit")
SPEC_INLINE_MAX = 30000
API_PAUSE = "FORGE: paused (API unreachable)"
API_TEXT = ("can't reach the api server", "connection_failed", "enotfound", "econnreset", "etimedout", "eai_again")
API_5XX = re.compile(r"\b(?:api error|http|status)\W{0,3}5\d\d\b|\b5\d\d (?:internal server|bad gateway|service unavailable"
                     r"|gateway time)", re.IGNORECASE)
BACKOFF_START_S, BACKOFF_CAP_S = 30, 900
EPOCH = re.compile(r"\|\s*(\d{10})\b")
CLOCK = re.compile(r"\bresets?(?:\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)\b", re.IGNORECASE)
YAMLKIT_ID = re.compile(r"\bid:\s*[\"']?((?:AS|AC|FR)-\d+)\b")
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
- Do not run the formatter (FORMAT_FIX); the driver formats changed files after your turn. Before finishing run every
  gate until all exit 0:
GATES
Finish with one line: BUILD: done"""

FIX = """The deterministic check failed. Fix the causes (same rules: frozen tests untouched, gates never weakened),
re-run the gates until green, then finish with: BUILD: done
If a frozen test contradicts the spec, append `- <test>: <why, with the spec quote>` to `.forge/disputes.md` instead.
Guard violations (`guard` in the output) must be removed, not worked around; a suppression that is truly required
goes to `.forge/disputes.md` with the reason.
"""
FIX_OUTPUT = "\nforge_check output:\n"

ADVISOR_NOTE = ("\n\nAn advisor model is available (the `advisor` tool). Consult it before committing to an approach, "
                "when the same gate fails twice, and before declaring the work done.")


def advisor_off(value: object) -> bool:
    return not value or str(value).strip().lower() in ("off", "none", "false", "no")


DEFAULT_BUILD_MODEL = "sonnet"


def cache_env(ttl: object) -> dict[str, str]:
    """Prompt-cache TTL of a `claude -p` session: `5m` (default; unattended sessions are short and continuous, a 1h
    cache write costs 1.6x a 5m one) or `1h`."""
    env = {k: v for k, v in os.environ.items() if k not in ("FORCE_PROMPT_CACHING_5M", "ENABLE_PROMPT_CACHING_1H")}
    if str(ttl or "5m").strip().lower() == "1h":
        env["ENABLE_PROMPT_CACHING_1H"] = "1"
    else:
        env["FORCE_PROMPT_CACHING_5M"] = "1"
    return env


def advisor_settings(advisor: str | None, where: Path) -> list[str]:
    """`--settings <file>` that turns the Claude Code advisor on for one `claude -p` session (a file, not inline JSON:
    the Windows driver runs through cmd.exe)."""
    if advisor_off(advisor):
        return []
    where.mkdir(parents=True, exist_ok=True)
    p = where / "advisor-settings.json"
    p.write_text(json.dumps({"advisorModel": str(advisor).strip()}), encoding="utf-8")
    return ["--settings", str(p)]


def advisor_event(ev: object, tally: dict) -> None:
    """Counts advisor calls and failed advisor results in one stream-json event."""
    if not isinstance(ev, dict) or ev.get("type") != "assistant":
        return
    for c in (ev.get("message") or {}).get("content") or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "server_tool_use" and c.get("name") == "advisor":
            tally["calls"] = tally.get("calls", 0) + 1
        elif c.get("type") == "advisor_tool_result":
            body = c.get("content")
            kind = str(body.get("type", "")) if isinstance(body, dict) else ""
            if "error" in kind:
                tally["errors"] = tally.get("errors", 0) + 1
                tally["last_error"] = str(body.get("error_code") or kind) if isinstance(body, dict) else kind

FIX_HANDOFF = """
You take over an unattended build from another session. Spec: `.forge/spec.md` (read it first); question lines and
defaults: `.forge/requirements.md`; disputes so far: `.forge/disputes.md` (may be absent). Frozen acceptance tests (never
edit them):
FROZEN
Changes so far (`git diff --stat START_SHA`):
DIFFSTAT
Do not run the formatter (FORMAT_FIX); the driver formats changed files after your turn.
Gates (all must exit 0):
GATES
Work in few turns: put independent tool calls (reads of different files, searches, independent commands) in ONE
message so they run in parallel; read each file once (by ranges if large); do not re-run a check that already passed
on an unchanged tree.
"""

PLAIN_TESTS = """Turn 1 of 2 of an unattended build (the spec follows). Do NOT write production code in this turn.
Before coding:
(a) List the ambiguities of the spec as question lines in `.forge/requirements.md`, one per line, exactly:
    `Q<n> [blocking|non-blocking|info]: <question> | default: <the chosen default> | cost of a wrong guess: <one phrase>`
    `blocking` ONLY when a wrong guess would make the whole unit worthless or irreversible (data loss, external side
    effects, public API shape); everything else gets a default and is `non-blocking`; `info` = a noteworthy assumption.
    Repeat the lines in your final message.
(b) Write executable acceptance tests that cover every requirement of the spec through the public interface
    (HTTP/CLI/public API) in the repo's test layout, as NEW files only (never edit an existing file). Name the
    requirement in the test name or a comment, assert exact values from the spec; the tests must be collectable and
    fail until the spec is implemented. List every new test file in `.forge/acceptance-files.txt`, one repo-relative
    path per line.
(c) Prove the tests are red: run TEST_RUN and confirm every new acceptance test FAILS on the current tree because
    the behaviour is missing (an assertion or a missing entry point, not a syntax or collection error). A test that
    already passes asserts nothing new: tighten it (exact values from the spec) until it fails. The driver checks
    this too and a vacuous suite costs another round.
Work in few turns: put independent tool calls (reads of different files, searches, independent commands) in ONE
message so they run in parallel; read each file once (by ranges if large); do not re-run a check that already passed
on an unchanged tree.
Then stop: no production code. Finish with one line: TESTS: done

SPEC:
"""

PLAIN_ANSWERS = """Human decisions arrived for your questions (binding). Update your acceptance tests accordingly: you may
edit the test files you created in turn 1, nothing else, and write no production code. Finish with one line: TESTS: done"""

PLAIN_COVER = """These requirement ids of the spec are not named in any acceptance test yet: IDS. Add acceptance tests for
them (new files, or the test files you created in this run), naming the id in the test name or a comment. No
production code. Finish with one line: TESTS: done"""

PLAIN_BUILD = """Turn 2 of 2. Implement the spec. A previous session wrote the acceptance tests and they are frozen (do not
edit, delete or skip):
FROZEN
Question lines and their defaults: `.forge/requirements.md`. SPEC_REF
Rules:
- Implement everything so that every frozen test passes; add unit tests for non-trivial rules.
- If a frozen test contradicts the spec, do not work around it: append `- <test>: <why, with the spec quote>` to
  `.forge/disputes.md` and implement the spec.
- Follow existing conventions; no unrelated refactors; never weaken a gate (no disabled rules, no skipped tests, no
  config edits).
- Work in few turns: put independent tool calls (reads of different files, searches, independent commands) in ONE message so they run in parallel; read each file once (by ranges if large); do not re-run a check that already passed on an unchanged tree.
- Do not run the formatter (FORMAT_FIX); the driver formats changed files after your turn. Before finishing run every
  gate until all exit 0:
GATES
Finish with one line: BUILD: done"""

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


def note_rate(info: dict) -> None:
    """Latest rate-limit state of the account for the caller (queue_run throttles parallel units on it)."""
    path = os.environ.get("POWERPUFF_RATE_FILE")
    if not path or not isinstance(info, dict):
        return
    win = (info.get("unifiedWindows") or {}).get(info.get("rateLimitType") or "five_hour") or {}
    data = {"status": info.get("status"), "type": info.get("rateLimitType"), "resets_at": info.get("resetsAt"),
            "utilization": win.get("utilization"), "at": int(time.time())}
    try:
        tmp = Path(f"{path}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


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


def load_profile(root: Path, path: Path | None = None) -> dict | None:
    """The human-owned gate profile (powerpuff.yaml / powerpuff.gates.json) or None; ValueError = configuration error.
    `path` reads a draft profile from elsewhere (never approved)."""
    if path is None and not any((root / n).is_file() for n in PROFILE_FILES):
        return None
    try:
        cfg = gate_guard.load_config(root, str(path) if path else None)
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
            "format_fix": str(cfg["format_fix"]) if cfg.get("format_fix") else None,
            "inner_loop": inner_loop_value(cfg), "config": cfg}


INNER_OFF = {"off", "false", "no", "none", "0"}


def inner_loop_value(cfg: dict) -> str | None:
    """Profile `inner_loop`: a command, `off`/false to disable, absent -> None (the default inner loop applies)."""
    if "inner_loop" not in cfg or cfg["inner_loop"] is None:
        return None
    raw = cfg["inner_loop"]
    if raw is False or str(raw).strip().lower() in INNER_OFF or not str(raw).strip():
        return "off"
    return str(raw)


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


class ApiUnreachable(Exception):
    """A session that ended on a transport/server error of the API: no model work was done, the code is not judged."""

    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


def failure_kind(result: dict | None, text: str, tail: list[str], api_msg: bool = False) -> str | None:
    """`rate` | `api` | None for a session that ended without a normal result. Structured fields first, text as
    fallback; a rate limit wins when both match."""
    if result and not result.get("is_error"):
        return None
    low = (text + "\n" + "\n".join(tail)).lower()
    if any(p in low for p in RATE):
        return "rate"
    r = result or {}
    if r.get("api_error") or r.get("terminal_reason") == "api_error" or api_msg \
            or any(p in low for p in API_TEXT) or API_5XX.search(low):
        return "api"
    return None


def backoff_s(attempt: int) -> float:
    return float(min(BACKOFF_START_S * 2 ** max(attempt - 1, 0), BACKOFF_CAP_S))


class Outage:
    """Backoff bookkeeping for API outages: pauses grow 30 s, 60 s, ... up to 15 min, the sum is bounded."""

    def __init__(self, max_min: float):
        self.max_s = max_min * 60
        self.waited = 0.0
        self.attempt = 0

    def next_wait(self, remaining_s: float = float("inf")) -> float | None:
        """Seconds to sleep before the next attempt; None when the outage limit or `remaining_s` is spent."""
        self.attempt += 1
        wait = min(backoff_s(self.attempt), self.max_s - self.waited, remaining_s)
        if wait <= 0:
            return None
        self.waited += wait
        return wait

    def healthy(self) -> None:
        self.attempt = 0


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
        self.state.setdefault("engine", "forge" if self.state["stage"] != "init" else getattr(a, "engine", "forge"))
        self.engine: str = self.state["engine"]
        self.outage = Outage(getattr(a, "max_outage_min", 120))
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

    def unit_files(self) -> list[str]:
        """Repo-relative files the unit changed since its start (tracked vs start_sha, plus untracked), `.forge/` out."""
        start = self.state.get("start_sha") or "HEAD"
        out = self.git("-c", "core.quotepath=off", "diff", "--name-only", "--no-renames", "-z", start) + "\0" \
            + self.git("-c", "core.quotepath=off", "ls-files", "--others", "--exclude-standard", "-z")
        files = [x for x in out.split("\0") if x and not x.startswith(".forge/") and (self.root / x).is_file()]
        return list(dict.fromkeys(files))

    def driver_format(self) -> None:
        """The profile `format_fix` run by the driver after a build / fix session, scoped to the unit's files for
        `dotnet format`; a failing formatter is logged, the gates decide."""
        fmt = self.state.get("format_fix")
        if not fmt:
            return
        cmd = forge_check.scope_format(forge_check.expand(str(fmt), self.gate_ctx()), self.unit_files())
        if cmd is None:
            return
        ctx = self.gate_ctx()
        try:
            p = subprocess.run(cmd, shell=True, cwd=self.root, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=max(self.a.gate_timeout, 900), env=forge_check.context_env(ctx))
            if p.returncode:
                self.log(f"format_fix exited {p.returncode}: {(p.stdout + p.stderr)[-300:].strip()}")
        except (subprocess.TimeoutExpired, OSError) as e:
            self.log(f"format_fix failed: {e}")

    def format_commit(self, msg: str) -> None:
        self.driver_format()
        self.commit(msg)

    def commit_frozen(self, msg: str) -> None:
        self.commit(msg)
        self.state["frozen_sha"] = self.git("rev-parse", "HEAD")
        self.save()

    def session(self, stage: str, model: str, prompt: str, resume: str | None = None,
                advisor: str | None = None) -> dict:
        """One `claude -p` session. Returns {"ok", "text", "session_id"}; raises RateLimited."""
        if time.monotonic() > self.deadline:
            raise SystemExit(self.finish("failed", "wall-clock budget exhausted"))
        if self.state["cost"]["total_usd"] >= self.a.max_usd:
            raise SystemExit(self.finish("failed", "cost budget exhausted"))
        argv = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", model,
                "--permission-mode", self.a.permission_mode, "--strict-mcp-config"]
        argv += ["--resume", resume] if resume else []
        adv = advisor_settings(advisor, self.forge)
        argv += adv
        prompt += ADVISOR_NOTE if adv else ""
        tally: dict = {}
        self.log(f"{stage}: session start ({model}{', resume' if resume else ''}"
                 f"{f', advisor {advisor}' if adv else ''})")
        proc = subprocess.Popen(argv, cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                shell=sys.platform == "win32", env={**cache_env(self.cache_ttl()), "POWERPUFF_DRIVER": "1"})
        assert proc.stdin and proc.stdout
        proc.stdin.write(prompt)
        proc.stdin.close()
        result, tail, api_msg, limited = None, [], False, 0
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                tail = (tail + [line.strip()])[-20:]
                continue
            advisor_event(ev, tally)
            if isinstance(ev, dict) and ev.get("type") == "rate_limit_event":
                info = ev.get("rate_limit_info") or {}
                note_rate(info)
                if info.get("status") == "rejected":
                    limited = int(info.get("resetsAt") or 0) or int(time.time()) + 900
            if isinstance(ev, dict) and ev.get("type") == "assistant" and ev.get("error") == "rate_limit":
                limited = limited or int(time.time()) + 900
            elif isinstance(ev, dict) and ev.get("type") == "assistant" and ev.get("is_api_error_message"):
                api_msg = True
            if isinstance(ev, dict) and ev.get("type") == "result":
                result = ev
                continue  # not forwarded: the caller gets ONE synthetic result at the end
            if self.a.stream:
                sys.stdout.write(line if line.endswith("\n") else line + "\n")
                sys.stdout.flush()
        proc.wait()
        r = result or {}
        cost = self.state["cost"]
        # `claude -p --resume` reports the session's cumulative cost: count only the increment
        cum = cost.setdefault("session_cum", {})
        prev = cum.get(resume) or {} if resume else {}
        reported = float(r.get("total_cost_usd") or 0)
        spent = max(0.0, reported - float(prev.get("usd", 0)))
        cost["total_usd"] = round(cost["total_usd"] + spent, 4)
        cost["by_stage"][stage] = round(cost["by_stage"].get(stage, 0) + spent, 4)
        cost["sessions"] += 1
        cost["turns"] += int(r.get("num_turns") or 0)
        models = {m: float(u.get("costUSD") or 0) for m, u in (r.get("modelUsage") or {}).items()}
        for m, v in models.items():
            inc = max(0.0, v - float((prev.get("models") or {}).get(m, 0)))
            cost["by_model"][m] = round(cost["by_model"].get(m, 0) + inc, 4)
        if r.get("session_id"):
            cum[r["session_id"]] = {"usd": reported, "models": models}
        self.save()
        raw = r.get("result")
        text: str = raw if isinstance(raw, str) else ""
        kind = failure_kind(result, text, tail, api_msg)
        if limited and (kind or not result or r.get("is_error")):
            raise RateLimited(f"{text}\n" + "\n".join(tail) + f"\nrate limit rejected, resets |{limited}")
        if kind == "rate":
            raise RateLimited(text + "\n" + "\n".join(tail))
        if kind == "api":
            raise ApiUnreachable(text + "\n" + "\n".join(tail))
        self.outage.healthy()
        ok = bool(result) and not r.get("is_error")
        if adv:
            st = self.state.setdefault("advisor", {"model": advisor, "calls": 0, "errors": 0})
            st["calls"] += tally.get("calls", 0)
            st["errors"] += tally.get("errors", 0)
            self.save()
            if tally.get("errors"):
                self.log(f"{stage}: WARNING advisor unavailable ({tally.get('last_error')}), continuing without it")
        self.log(f"{stage}: session end ok={ok} cost={spent:.2f} turns={r.get('num_turns')}"
                 + (f" advisor_calls={tally.get('calls', 0)}" if adv else "")
                 + ("" if ok else " error=" + " ".join(text.split())[:200] + " tail=" + " | ".join(tail[-3:])[:300]))
        return {"ok": ok, "text": text, "session_id": r.get("session_id")}

    def staged(self, stage: str, model: str, prompt: str, done, resume: str | None = None, tries: int = 2,
               advisor: str | None = None) -> dict:
        """Runs a session until `done()` holds (deterministic check), sleeping through rate limits."""
        out: dict = {"ok": False, "text": "", "session_id": resume}
        left = tries
        while left > 0:
            try:
                out = self.session(stage, model, prompt, resume, advisor=advisor)
            except RateLimited as e:
                if self.a.no_wait:
                    self.emit_result("rate limit: " + e.text[-300:], is_error=True)
                    raise SystemExit(3) from None
                wait = min(reset_wait_s(e.text), max(0.0, self.deadline - time.monotonic()))
                self.log(f"{stage}: rate limited, sleeping {wait / 60:.0f} min")
                time.sleep(wait)
                if not resume and "inspect the tree" not in prompt:
                    prompt += "\n\nA previous attempt was interrupted; inspect the tree and continue."
                continue  # a rate limit does not consume a try
            except ApiUnreachable as e:
                self.api_wait(stage, e.text)
                continue  # an API outage does not consume a try either
            left -= 1
            if out["ok"] and done():
                return out
            if out["ok"]:
                resume = out.get("session_id")
                prompt = "Not finished: the required output files are missing or invalid. Complete the task."
        return out

    def api_wait(self, stage: str, text: str) -> None:
        """Sleeps the next backoff step of an API outage; without --no-wait budget left (or with --no-wait at once)
        the run pauses: exit 3, state untouched, not a verdict about the code."""
        wait = None if self.a.no_wait else self.outage.next_wait(self.deadline - time.monotonic())
        if wait is None:
            self.log(f"{stage}: API unreachable, pausing after {self.outage.waited / 60:.0f} min of waiting")
            self.log(API_PAUSE)
            (self.forge / "REPORT.md").write_text(
                "# FORGE REPORT - PAUSED (API unreachable)\n\nThe API could not be reached; no model work was lost or judged.\n"
                "This is NOT a verdict about the code: rerun the same command, the run continues at stage "
                f"`{self.state['stage']}`.\n\n- last error: {' '.join(text.split())[-300:]}\n"
                f"- waited for the API: {self.outage.waited / 60:.0f} min\n", encoding="utf-8")
            self.emit_result(f"report: .forge/REPORT.md\n{API_PAUSE}\napi error: " + " ".join(text.split())[-300:],
                             is_error=True, forge_outcome="paused-api")
            if not self.a.stream:
                print(API_PAUSE, flush=True)
            raise SystemExit(3)
        self.log(f"{stage}: API unreachable, retry in {wait:.0f} s (attempt {self.outage.attempt})")
        time.sleep(wait)

    def emit_result(self, text: str, is_error: bool = False, **extra: str) -> None:
        if not self.a.stream:
            return
        c = self.state["cost"]
        ev = {"type": "result", "subtype": "success", "is_error": is_error, "result": text,
              "total_cost_usd": c["total_usd"], "num_turns": c["turns"], "session_id": None,
              "modelUsage": {m: {"costUSD": v} for m, v in c["by_model"].items()}, **extra}
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
        plain = self.engine == "plain"
        self.state.update(stage="plain-tests" if plain else "audit", start_sha=self.git("rev-parse", "HEAD"),
                          writer=self.a.model if plain else self.a.writer)
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
        if not prof and getattr(self.a, "engine", "forge") == "plain":
            prof = self.preset_draft()
        if not prof:
            return False
        self.profile = prof["config"]
        setup = (prof["config"] or {}).get("setup")
        self.state.update(final_gates=prof["final"], final_commands=prof["final_gates"],
                          profile_approved=prof["approved"], setup=setup if isinstance(setup, str) and setup.strip() else None)
        excluded = set(self.state.get("gates_excluded_red_on_base") or []) if not prof["approved"] else set()
        self.fix_gates({n: c for n, c in prof["gates"].items() if n not in excluded}, prof["format_fix"], "profile")
        return True

    def preset_draft(self) -> dict | None:
        """No powerpuff.yaml: draft the profile from the stack presets (profile_init) into .forge/; never approved."""
        import profile_init
        draft_path = self.forge / "powerpuff.draft.yaml"
        if not draft_path.is_file():
            projects = profile_init.find_projects(self.root, profile_init.load_presets())
            draft = profile_init.build(self.root, projects) if projects else None
            if not draft or not draft["commands"]:
                return None
            self.forge.mkdir(parents=True, exist_ok=True)
            draft_path.write_text(profile_init.render(projects, draft), encoding="utf-8")
            self.log(f"profile: none in the repo, drafted from presets -> {draft_path.relative_to(self.root).as_posix()}")
        try:
            prof = load_profile(self.root, draft_path)
        except ValueError as e:
            self.log(f"profile draft unusable: {e}")
            return None
        if prof:
            prof["approved"] = False
        return prof

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

    def run_setup(self, stage: str) -> None:
        """Profile `setup` (install dependencies) once, before the base check: gates on a fresh clone need it."""
        cmd = self.state.get("setup")
        if not cmd or self.state.get("setup_done"):
            return
        r = forge_check.check_gates(self.root, max(self.a.gate_timeout, 900), {"setup": cmd},
                                    self.gate_ctx(head=True))["results"]["setup"]
        self.state["setup_done"] = True
        self.save()
        if r["ok"]:
            self.log(f"setup: ok ({r.get('seconds', 0)} s)")
            return
        if self.state.get("profile_approved"):
            self.state["base_red_tails"] = {"setup": r.get("tail", "")}
            raise SystemExit(self.fail_resumable(f"setup failed: {cmd}", stage))
        self.state["setup_failed"] = cmd
        self.log(f"WARNING: draft profile setup failed, continuing: {cmd}")

    def base_check(self, stage: str) -> None:
        """All active gates once on the untouched base tree, before anything is spent on it."""
        if "base_checked" in self.state:
            return
        if self.a.skip_base_check:
            self.run_setup(stage)
            self.state["base_checked"] = "skipped"
            self.save()
            return
        self.run_setup(stage)
        gates: dict = self.state.get("gates") or {}
        res = self.note_gates(forge_check.check_gates(self.root, self.a.gate_timeout, gates,
                                                      self.gate_ctx(head=True))["results"]) if gates else {}
        draft = self.state.get("gates_source") == "profile" and not self.state.get("profile_approved")
        for name, r in res.items():
            if r.get("missing_tool") and not draft:  # a draft guessed the tool: handled as red on base below
                words = gates[name].split()
                self.state["base_red_tails"] = {name: r.get("tail", "")}
                raise SystemExit(self.fail_resumable(
                    f"base check: missing tool '{words[0] if words else gates[name]}' (gate '{name}')", stage))
        red = {n: r.get("tail", "") for n, r in res.items() if not r["ok"]}
        if red and draft and len(red) < len(gates):
            # an unapproved preset draft guessed commands that do not work on this repo: drop them, say so loudly
            self.state["base_red_tails"] = red
            self.state["gates_excluded_red_on_base"] = sorted(red)
            self.fix_gates({n: c for n, c in gates.items() if n not in red}, self.state.get("format_fix"), "profile")
            self.log(f"WARNING: draft profile gates red on the untouched base, excluded: {', '.join(sorted(red))}")
            red = {}
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

    def questions_gate(self, stage: str = "tests") -> None:
        """A blocking question without an answer is never guessed, unless --assume-defaults says so."""
        if self.state.get("questions_ok"):
            return
        if not (self.forge / "questions.json").is_file():
            self.write_questions()
        qs = self.load_questions()
        if qs is None:
            raise SystemExit(self.fail_resumable("questions.json is not a JSON list of questions", stage))
        open_q = [q for q in qs if q.get("class") == "blocking" and not answered(q)]
        if open_q and not self.a.assume_defaults:
            self.log("answer the blocking questions in .forge/questions.json (`answer`) and rerun, or pass --assume-defaults")
            raise SystemExit(self.fail_resumable(f"blocking questions: {','.join(str(q.get('id')) for q in open_q)}",
                                                 stage))
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
            sid = self.state.get("sid_plain")  # plain engine: the same session, forge: a fresh one
            out = self.staged("tests-vacuous", self.state["writer"], VACUOUS + self.human_decisions(),
                              lambda: bool(set(self.acceptance_files()) - known), resume=sid)
            if sid and out.get("session_id"):
                self.state["sid_plain"] = out["session_id"]
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

    def tree_key(self) -> str:
        """Identity of the tree the gates would see: HEAD, the dirty paths (with mtime and size) and the gate set."""
        h = hashlib.sha256(self.git("rev-parse", "HEAD").encode())
        for ln in sorted(self.git("status", "--porcelain", "-uall", "--", ".", ":!.forge").splitlines()):
            path = re.sub(r"^\S{1,2}\s+", "", ln.strip()).rsplit(" -> ", 1)[-1].strip('"')  # git() strips line 1
            try:
                st = (self.root / path).stat()
                stamp = f"{st.st_mtime_ns}:{st.st_size}"
            except OSError:
                stamp = "-"
            h.update(f"{ln}\0{stamp}\0".encode())
        h.update(json.dumps(self.state.get("gates") or {}, sort_keys=True).encode())
        return h.hexdigest()

    def check(self) -> dict:
        """forge_check on the current tree; the same tree is never checked twice in one process (a no-change fix
        round and `finish` right after a check reuse the last result instead of re-running every gate)."""
        key = self.tree_key()
        cached = getattr(self, "_check_cache", None)
        if cached and cached[0] == key:
            rep = json.loads(json.dumps(cached[1]))
            self.log(f"check: tree unchanged since the last check, result reused (done={rep['done']})")
            return rep
        rep = self.run_check()
        # a check that restored a tampered frozen test reports frozen=False for a tree that no longer exists: the
        # restored tree must be checked afresh next time, otherwise a no-change fix round reuses the stale failure
        self._check_cache = None if rep.get("frozen_restored") else (self.tree_key(), json.loads(json.dumps(rep)))
        return rep

    def run_check(self) -> dict:
        gates_tampered = self.restore_gate_config()
        files = forge_check.frozen_files(self.root)
        tampered = forge_check.check_frozen(self.root, files)["changed_or_missing"]
        for f in tampered:  # a tampered frozen test is restored before the gates run, never accepted
            self.git("checkout", self.state.get("frozen_sha", "HEAD"), "--", f)
        if tampered:
            self.state["frozen_violations"] = int(self.state.get("frozen_violations", 0)) + len(tampered)
        self.save()  # the checker sees the driver's state, not what a session left in state.json
        chk = forge_check.check_gates(self.root, self.a.gate_timeout, self.state.get("gates") or {}, self.gate_ctx(),
                                      changed=self.unit_files())
        self.note_gates(chk["results"])
        rep: dict = {"coverage": forge_check.check_coverage(self.root, files),
                     "frozen": forge_check.check_frozen(self.root, files), "gates": chk, "guard": self.guard()}
        if self.engine == "plain":  # spec R ids named in the frozen tests; a gap is reported, not fixable after the freeze
            rep["coverage"] = {**self.spec_coverage(files), "ok": True}
        if tampered:
            rep["frozen"] = {"ok": False, "files": len(files), "changed_or_missing": tampered,
                             "note": "restored from git; frozen acceptance tests must not be edited"}
            rep["frozen_restored"] = True
        if gates_tampered:
            rep["gates_tampered"] = True
        rep["done"] = all(rep[k]["ok"] for k in ("coverage", "frozen", "gates", "guard")) and not gates_tampered
        (self.forge / "check.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
        gates = {k: v["ok"] for k, v in rep["gates"].get("results", {}).items()}
        self.log(f"check: done={rep['done']} coverage={rep['coverage']['ok']} frozen={rep['frozen']['ok']} "
                 f"gates={gates} guard={rep['guard']['ok']}")
        return rep

    def build_prompt(self) -> str:
        gates, fmt = self.gate_lines()
        return BUILD.replace("FORMAT_FIX", fmt).replace("GATES", gates) \
            + self.human_decisions()

    def inner_loop(self) -> str | None:
        """Fast check the session iterates on (profile `inner_loop`, default = the test gates); the driver runs the full gates.
        `inner_loop: off` in the profile restores the old behaviour (the session runs every gate)."""
        cmd = inner_loop_value(self.profile or {}) if self.state.get("gates_source") == "profile" else None
        if cmd == "off":
            return None
        if not cmd:
            tests = list(self.test_gates().values())
            if not tests:
                return None
            cmd = tests[0] if len(tests) == 1 else " && ".join(f"({c})" for c in tests)
        return forge_check.expand(cmd, self.gate_ctx())

    def inner_note(self) -> str:
        inner = self.inner_loop()
        return (f"\nIteration: run only the fast inner loop `{inner}` (and the formatter in fix mode) until it exits 0. "
                "Do NOT run the full gates yourself: the harness runs them right after you finish and sends any failure "
                "back to you.\n") if inner else ""

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
            self.format_commit(f"forge({stage}{i + 1})")
            rep = self.check()
            self.state[key] = sid
            self.state[f"round_{stage}"] = i + 1
            self.save()
            if rep["done"]:
                return True
            prompt = FIX + self.inner_note() + FIX_OUTPUT + failure_brief(rep)
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
            self.format_commit(f"forge({stage}-escalation)")
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

    # ---- plain engine -----------------------------------------------------------------------------
    def plain_turn(self, stage: str, prompt: str, done=lambda: True) -> dict:  # noqa: B008
        """One turn in the single model context: resumes `sid_plain`, remembers the session id of the answer."""
        out = self.staged(stage, self.model_for("tests"), prompt, done, resume=self.state.get("sid_plain"))
        if out.get("session_id"):
            self.state["sid_plain"] = out["session_id"]
            self.save()
        return out

    def plain_new_tests(self, before: dict[str, dict]) -> list[str]:
        """New test files of the turn: untracked, not dirty before it, listed by the model or recognised as tests."""
        try:
            by_ext = gate_guard.build_languages(self.profile or {})
        except gate_guard.GuardError:
            by_ext = gate_guard.build_languages({})
        listed = set(self.acceptance_files())
        return sorted(f for f in self.changed_paths()[1] if f not in before["untracked"]
                      and (f in listed or gate_guard.is_test_file(f, by_ext.get(gate_guard.ext(f)))))

    def plain_before(self) -> dict[str, dict]:
        """The dirty tree as it was before turn 1 (kept in state: a killed run guards against the same picture)."""
        if "plain_before" not in self.state:
            snap = self.dirty_snapshot()
            self.state["plain_before"] = {
                "tracked": {f: None if b is None else b.hex() for f, b in snap["tracked"].items()},
                "untracked": snap["untracked"]}
            self.save()
        raw = self.state["plain_before"]
        return {"tracked": {f: None if b is None else bytes.fromhex(b) for f, b in raw["tracked"].items()},
                "untracked": raw["untracked"]}

    def spec_coverage(self, files) -> dict:
        """Requirement ids of the spec that no acceptance test names: `R<n>` lines (`R1: ...` / `- R1: ...`) and yamlkit
        ids (`id: "AS-1"` acceptance scenarios, `AC-n` acceptance criteria, `FR-nnn` functional requirements)."""
        spec = self.forge / "spec.md"
        raw = spec.read_text(encoding="utf-8") if spec.is_file() else ""
        found = set(forge_check.R_LINE.findall(raw)) | set(YAMLKIT_ID.findall(raw))
        ids = sorted(found, key=lambda x: (re.sub(r"[-\d]", "", x), int(re.sub(r"\D", "", x))))
        text = "\n".join((self.root / f).read_text(encoding="utf-8", errors="replace")
                         for f in files if (self.root / f).is_file())
        return {"requirements": len(ids),
                "uncovered": [r for r in ids if not re.search(rf"(?<![\w-]){re.escape(r)}(?![\w])", text)]}

    def write_frozen_json(self) -> None:
        """Run-scoped protected list for hook_protect: sessions of this run cannot edit the frozen tests."""
        files = forge_check.frozen_files(self.root)
        if files:
            (self.forge / "frozen.json").write_text(json.dumps({"files": files}, indent=1), encoding="utf-8")

    def plain_tests(self) -> None:
        before = self.plain_before()
        if not self.state.get("plain_turn1"):
            ctx = self.gate_ctx()
            runs = [f"`{forge_check.expand(c, ctx)}`" for c in self.test_gates().values()]
            test_run = " and ".join(runs) if runs else "the repo's test command for the new files"
            out = self.plain_turn("plain-tests", PLAIN_TESTS.replace("TEST_RUN", test_run)
                                  + (self.forge / "spec.md").read_text(encoding="utf-8"),
                                  lambda: bool(self.plain_new_tests(before)))
            self.keep_additive("plain-tests", before)
            req_path = self.forge / "requirements.md"
            cur = req_path.read_text(encoding="utf-8") if req_path.is_file() else ""
            qs = [x for x in out["text"].splitlines() if Q_LINE.match(x)]
            if qs and not any(Q_LINE.match(x) for x in cur.splitlines()):
                req_path.write_text(cur.rstrip("\n") + "\n" + "\n".join(qs) + "\n", encoding="utf-8")
            self.write_questions()
            self.state["plain_turn1"] = True
            self.save()
        self.questions_gate("plain-tests")
        if self.human_decisions() and not self.state.get("plain_decisions_sent"):
            self.plain_turn("plain-tests-answers", PLAIN_ANSWERS + self.human_decisions())
            self.keep_additive("plain-tests-answers", before)
            self.state["plain_decisions_sent"] = True
            self.save()
        tests = self.plain_new_tests(before)
        if not tests:
            raise SystemExit(self.finish("failed", "no acceptance tests written"))
        missing = self.spec_coverage(tests)["uncovered"]
        if missing and not self.state.get("plain_cover_sent"):
            self.state["plain_cover_sent"] = True
            self.save()
            self.plain_turn("plain-tests-cover", PLAIN_COVER.replace("IDS", ", ".join(missing)))
            self.keep_additive("plain-tests-cover", before)
            tests = self.plain_new_tests(before)
        (self.forge / "acceptance-files.txt").write_text("\n".join(tests) + "\n", encoding="utf-8")
        self.freeze(tests)
        self.commit_frozen("forge: acceptance tests")
        self.write_frozen_json()
        if "red_on_base" not in self.state:
            self.red_on_base()
        self.state["stage"] = "plain-build"
        self.save()

    def gate_lines(self) -> tuple[str, str]:
        ctx = self.gate_ctx()
        gates = "\n".join(f"  - {n}: `{forge_check.expand(c, ctx)}`" for n, c in (self.state.get("gates") or {}).items())
        inner = self.inner_loop()
        if inner:
            gates = (f"  - inner loop (YOU run this one, it is fast): `{inner}`\n"
                     "  The gates below are run by the harness after you finish - do not run them yourself:\n" + gates)
        return gates, forge_check.expand(str(self.state.get("format_fix") or "none"), ctx)

    def frozen_lines(self) -> str:
        return "\n".join(f"  - {f}" for f in forge_check.frozen_files(self.root))

    def spec_ref(self) -> str:
        spec = self.forge / "spec.md"
        text = spec.read_text(encoding="utf-8") if spec.is_file() else ""
        if len(text) <= SPEC_INLINE_MAX:
            return "The spec (also in `.forge/spec.md`):\n\n" + text.strip() + "\n"
        return "The spec is in `.forge/spec.md`: read it first, then the files it points to as needed."

    def model_for(self, step: str) -> str:
        """plain engine: `tests` / `build` / `fix` model = CLI, else profile `model_tests` / `model_build` / `model_fix`,
        else tests -> the main model, build -> sonnet (the main model when it is not opus/fable), fix -> the build
        model."""
        cli = getattr(self.a, f"model_{step}", None)
        prof = (self.profile or {}).get(f"model_{step}")
        if cli:
            return str(cli)
        if isinstance(prof, str) and prof.strip():
            return prof.strip()
        if step == "fix":
            return self.model_for("build")
        main = self.state["writer"]
        if step == "build" and any(m in main.lower() for m in ("opus", "fable")):
            return DEFAULT_BUILD_MODEL
        return main

    def cache_ttl(self) -> str:
        """Prompt-cache TTL: profile `cache_ttl`, else env `POWERPUFF_CACHE_TTL`, else 5m."""
        return str((self.profile or {}).get("cache_ttl") or os.environ.get("POWERPUFF_CACHE_TTL") or "5m")

    def advisor(self) -> str | None:
        """plain engine build/fix sessions: advisor model = CLI `--advisor`, else profile `advisor`, else the
        user default `POWERPUFF_ADVISOR`, else none."""
        cli = getattr(self.a, "advisor", None)
        prof = (self.profile or {}).get("advisor")
        val = cli if cli is not None else prof if prof is not None else os.environ.get("POWERPUFF_ADVISOR")
        return None if advisor_off(val) else str(val).strip()

    def fix_handoff(self) -> str:
        gates, fmt = self.gate_lines()
        stat = self.git("diff", "--stat", self.state["start_sha"], "--", ".", ":!.forge")[-3000:] or "(none)"
        return FIX_HANDOFF.replace("FROZEN", self.frozen_lines()).replace("START_SHA", self.state["start_sha"]) \
            .replace("DIFFSTAT", stat).replace("FORMAT_FIX", fmt).replace("GATES", gates)

    def plain_build(self) -> None:
        if not self.state.get("plain_built"):
            gates, fmt = self.gate_lines()
            prompt = PLAIN_BUILD.replace("FROZEN", self.frozen_lines()).replace("FORMAT_FIX", fmt) \
                .replace("GATES", gates).replace("SPEC_REF", self.spec_ref()) + self.human_decisions()
            sid = self.state.get("sid_build")
            if self.state.get("plain_build_started"):
                prompt = "Turn 2 of 2 (continued). A previous attempt was interrupted; inspect the tree and continue." \
                    if sid else prompt + "\n\nA previous attempt was interrupted; inspect the tree and continue."
            self.state["plain_build_started"] = True
            self.save()
            model = self.model_for("build")
            out = self.staged("plain-build", model, prompt, lambda: True, resume=sid, advisor=self.advisor())
            self.state.update(sid_build=out.get("session_id") or sid, sid_build_model=model)
            if not out["ok"]:
                self.commit("forge(plain-build, unfinished)")
                raise SystemExit(self.fail_resumable("build session ended with an error", "plain-build"))
            self.state["plain_built"] = True
            self.format_commit("forge(plain-build)")
            self.save()
        if self.state.pop("stuck_round", None) is not None:
            self.state["max_rounds_extra"] = int(self.state.get("max_rounds_extra", 0)) + 1
            self.save()
        rounds = self.a.max_fix_rounds + int(self.state.get("max_rounds_extra", 0))
        while True:
            rep = self.check()
            rnd = int(self.state.get("plain_round", 0))
            green = bool(rep["done"])
            if green or rnd >= rounds:
                break
            self.log(f"plain-build: red, fix round {rnd + 1}/{rounds}")
            head = self.git("rev-parse", "HEAD")
            self.fix_round(rnd + 1, rep)
            self.format_commit(f"forge(plain-fix{rnd + 1})")
            self.state["plain_round"] = rnd + 1
            self.save()
            if self.git("rev-parse", "HEAD") == head and not self.state.get("stuck_escalated"):
                # a resumed session that changed nothing often just agrees with itself: one fresh session with the
                # main model, the gate output and the disputes gets a try before a human is asked
                self.state["stuck_escalated"] = True
                self.save()
                self.log(f"plain-fix{rnd + 1}: no change to the tree - one escalation round in a fresh session")
                self.fix_round(rnd + 1, rep, escalate=True)
                self.format_commit(f"forge(plain-fix{rnd + 1}-esc)")
            if self.git("rev-parse", "HEAD") == head:
                self.log(f"plain-fix{rnd + 1}: no change to the tree - stuck, needs a human (see .forge/disputes.md)")
                if self.check()["done"]:
                    green = True
                    break
                self.state["stuck_round"] = rnd + 1
                raise SystemExit(self.fail_resumable(
                    f"stuck: fix round {rnd + 1} changed nothing; answer .forge/disputes.md "
                    f"(decision -> .forge/questions.json `answer`) and rerun", "plain-build"))
        self.state["build_green"] = green
        self.state["stage"] = "accept"
        self.save()

    def fix_round(self, n: int, rep: dict, escalate: bool = False) -> None:
        """Fix rounds continue the build session while the model stays the same; a different `model_fix` or the
        escalation of the last round (to the main model) starts a fresh session with a short handoff instead.
        `escalate` (a round that changed nothing): fresh session, main model, disputes in the prompt."""
        model = self.model_for("fix")
        if escalate or n >= self.a.max_fix_rounds and n > 1 and model != self.a.model:
            if model != self.a.model:
                self.log(f"plain-fix{n}: escalation to {self.a.model}")
            model = self.a.model
        sid = None if escalate else self.state.get("sid_build") if self.state.get("sid_build_model") == model else None
        disputes = self.forge / "disputes.md"
        note = ("\n\nThe previous round changed nothing. Disputes on record (decide them against the spec; human "
                "decisions below win):\n" + disputes.read_text(encoding="utf-8")[-3000:]) \
            if (escalate or self.state.get("max_rounds_extra")) and disputes.is_file() else ""
        if escalate:
            note += ("\n\nThe previous session ended without changing anything although the check is red. Look at the "
                     "failing gate output yourself, find the root cause and fix it in the code (never in the frozen "
                     "tests or the gate config).")
        brief = FIX_OUTPUT + failure_brief(rep) + note + self.human_decisions()
        prompt = FIX + self.inner_note() + (brief if sid else self.fix_handoff() + brief)
        out = self.staged(f"plain-fix{n}{'-esc' if escalate else ''}", model, prompt, lambda: True, resume=sid,
                          advisor=self.advisor())
        if out.get("session_id"):
            self.state.update(sid_build=out["session_id"], sid_build_model=model)
            self.save()

    def run_plain(self) -> int:
        if not self.adopt_profile():
            raise SystemExit(self.finish("failed", "plain engine needs a gate profile: no powerpuff.yaml and no preset matched the stack (see docs/profile.md)"))
        model = (self.profile or {}).get("model")
        if not getattr(self.a, "model_given", True) and isinstance(model, str) and model.strip():
            self.a.model = model.strip()
        self.state["writer"] = self.a.model
        self.save()
        self.write_frozen_json()
        if self.state["stage"] == "plain-tests":
            self.base_check("plain-tests")
            self.plain_tests()
        if self.state["stage"] == "plain-build":
            self.plain_build()
        if self.state["stage"] == "accept":
            if self.a.accept and self.state.get("build_green"):
                self.accept()
            self.state["stage"] = "report"
            self.save()
        if not self.state.get("build_green"):
            return self.finish("failed", f"gates red after {self.state.get('plain_round', 0)} fix round(s)")
        if self.a.final and not self.state.get("open_gaps"):
            self.final_tier()
        return self.finish("done")

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
        adv = self.state.get("advisor")
        if adv and not adv.get("calls"):
            self.log(f"WARNING advisor {adv.get('model')} was configured but never called (for fable: accept the "
                     f"usage-credit consent once via an interactive `/model fable`)")
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
        if self.state.get("setup_failed"):
            trust += f"; setup failed: {self.state['setup_failed']}"
        if self.state.get("gates_excluded_red_on_base"):
            trust += "; red on the untouched base, excluded: " + ", ".join(self.state["gates_excluded_red_on_base"])
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
                 *([f"- engine: plain, fix rounds used: {self.state.get('plain_round', 0)}"]
                   if self.engine == "plain" else []),
                 *([f"- test-writer changes reverted: {n_gone} file(s)"] if n_gone else []),
                 *([f"- frozen test violations (restored from git): {self.state['frozen_violations']}"]
                   if self.state.get("frozen_violations") else []),
                 *([f"- base was red (allowed by --allow-red-base): {', '.join(base_red)}"] if base_red else []),
                 f"- requirements: {rep.get('coverage', {}).get('requirements') or len(pick('R'))}, uncovered: {rep.get('coverage', {}).get('uncovered', 'n/a')}",
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
        (self.forge / "frozen.json").unlink(missing_ok=True)
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
        if self.engine == "plain":
            return self.run_plain()
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
    ap.add_argument("--engine", choices=("plain", "forge"), default="plain",
                    help="plain: one model context + script-enforced extras; forge: multi-session pipeline")
    ap.add_argument("--model", default=None, help="strong model, default opus (plain: also writes tests and code; "
                                                   "then the profile's `model` is the default)")
    ap.add_argument("--writer", default="sonnet", help="model that writes acceptance tests and code")
    ap.add_argument("--model-tests", default=None, help="plain: model of the test turns (default: profile `model_tests`, "
                                                         "else --model)")
    ap.add_argument("--model-build", default=None, help="plain: model of the build turn (default: profile `model_build`, "
                                                         "else sonnet when --model is opus/fable, else --model)")
    ap.add_argument("--model-fix", default=None, help="plain: model of fix rounds (default: profile `model_fix`, else "
                                                       "the build model); the last round escalates to --model")
    ap.add_argument("--advisor", default=None, help="plain: advisor model of build/fix sessions (fable|opus|sonnet|off; "
                                                     "default: profile `advisor`, else none)")
    ap.add_argument("--max-fix-rounds", type=int, default=4, help="plain: resumes with failing gates before giving up")
    ap.add_argument("--accept", action="store_true", help="plain: also run the independent acceptance pass at the end")
    ap.add_argument("--max-hours", type=float, default=8)
    ap.add_argument("--max-usd", type=float, default=60, help="cumulative notional budget across all sessions")
    ap.add_argument("--gate-timeout", type=int, default=1800)
    ap.add_argument("--permission-mode", default="bypassPermissions")
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--stream", action="store_true")
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument("--max-outage-min", type=float, default=120, help="total wait for an unreachable API before pausing")
    ap.add_argument("--allow-red-base", action="store_true", help="continue when gates are red on the untouched base")
    ap.add_argument("--assume-defaults", action="store_true", help="use defaults for unanswered blocking questions")
    ap.add_argument("--final", action="store_true", help="also run the profile tier `final` once after acceptance")
    ap.add_argument("--skip-base-check", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    a.model_given = a.model is not None
    a.model = a.model or "opus"
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    return Run(a).run()


if __name__ == "__main__":
    sys.exit(main())
