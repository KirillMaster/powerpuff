"""queue_run - outer script-orchestrator over a queue of small units of work. No model calls of its own.

Every unit runs through forge_run.py in its own git worktree and branch (cut from the current tip of the integration
branch); a failed or blocked unit never stops the run. State: <repo>/.powerpuff/runs/<run_id>/state.json (resumable).

Usage: python queue_run.py <queue.yaml> [--repo .] [--engine plain|forge] [--parallel N] [--no-wait] [--only U1,U2]
       [--dry-run] [--attended]
  --engine    forge_run engine of every unit; overrides queue.yaml `engine` (default plain)
  --parallel  run up to N independent units at once (overrides queue.yaml `parallel`, default 1); fewer slots while the
              account's 5-hour window is nearly used (rate-limit events of the sessions), none until a rejected window resets
  --no-wait   on a rate limit stop the run with exit 3 (state saved) instead of sleeping
  --max-outage-min  forge paused on an unreachable API (exit 3): wait 30 s, 60 s, ... <=15 min and rerun the unit;
              after N minutes in total (or with --no-wait at once) stop with exit 3, the unit stays `running`
  --dry-run   validate and print the execution order and the budget split, create nothing
  --attended  stop after every unit with exit 4 and `QUEUE: paused after <unit>`; re-run continues
Exit: 0 done, 1 failed, 2 partial, 3 config error / rate limit with --no-wait, 4 attended pause. Last line `QUEUE: ...`.
A failed unit gets `max_attempts` runs in total (default 2): the next one starts from a fresh branch and is told why the
previous one failed (plus its .forge/disputes.md). A unit whose fix round changed nothing (`stuck`) or that has blocking
questions stops as `needs-answer` without a retry. Units listed in `already_merged` are taken as merged into `base`.
Final contour (once, after the last merge): profile tier `final` of powerpuff.yaml merged with queue.yaml
`final.commands` (queue wins by name); commands may use {base} (run base_sha) and {root}. Format: docs/queue-format.md
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_check  # noqa: E402
import forge_run  # noqa: E402
import spec_slice  # noqa: E402

FORGE = Path(__file__).resolve().parent / "forge_run.py"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
RISKS = ("low", "high")
ENGINES = ("plain", "forge")
GOOD = ("merged", "done")
EXCLUDES = (".powerpuff/", ".forge/", ".forge-unit-spec.md")
PARALLEL_ENV = {"MSBUILDDISABLENODEREUSE": "1", "DOTNET_CLI_USE_MSBUILD_SERVER": "0"}  # worktrees must not share build nodes
RATE_FRESH_S = 900
IDENT = ["-c", "user.name=powerpuff", "-c", "user.email=powerpuff@local"]
sleep = time.sleep


def git(cwd: Path | str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.longpaths=true", *args], cwd=cwd,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def posix(p: Path | str) -> str:
    return str(p).replace(chr(92), "/")


def load_queue(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError:
        try:
            data = json.loads(text)
        except ValueError:
            raise ValueError("PyYAML is not installed: write queue.yaml as JSON or install PyYAML") from None
    else:
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("queue file must be a mapping")
    return data


def str_list(v: object) -> list[str] | None:
    if v is None:
        return []
    return [str(x) for x in v] if isinstance(v, list) else None


def closure(deps: dict[str, list[str]], uid: str) -> set[str]:
    seen: set[str] = set()
    stack = list(deps.get(uid, []))
    while stack:
        d = stack.pop()
        if d not in seen:
            seen.add(d)
            stack.extend(deps.get(d, []))
    return seen


def normalize(q: dict, qfile: Path, repo: Path) -> tuple[dict, list[str]]:
    """Returns (config, problems); every problem is listed, nothing is created."""
    errs: list[str] = []
    raw = qfile.read_bytes()
    run_id = str(q.get("run_id") or "") or \
        f"{datetime.fromtimestamp(qfile.stat().st_mtime):%Y-%m-%d}-{hashlib.sha1(raw).hexdigest()[:6]}"
    if not ID.fullmatch(run_id):
        errs.append(f"run_id '{run_id}' must match [A-Za-z0-9_.-]+")
    ib = str(q.get("integration_branch") or f"powerpuff/run-{run_id}")
    if ib == f"powerpuff/{run_id}" or ib.startswith(f"powerpuff/{run_id}/"):
        errs.append(f"integration_branch '{ib}' collides with the unit branches powerpuff/{run_id}/<unit>")
    budget = q.get("budget") or {}
    if not isinstance(budget, dict):
        errs.append("budget must be a mapping")
        budget = {}
    nums = {}
    for k, d in (("max_usd", 60), ("max_hours", 8), ("fix_reserve", 0.33)):
        v = budget.get(k, d)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or (k == "fix_reserve" and v >= 1) \
                or (k != "fix_reserve" and v <= 0):
            errs.append(f"budget.{k} has an invalid value: {v!r}")
            v = d
        nums[k] = float(v)
    unit_usd = budget.get("unit_usd")
    if unit_usd is not None and (isinstance(unit_usd, bool) or not isinstance(unit_usd, (int, float)) or unit_usd <= 0):
        errs.append(f"budget.unit_usd has an invalid value: {unit_usd!r}")
        unit_usd = None
    ints = {}
    for k, d in (("max_attempts", 2), ("parallel", 1)):
        v = q.get(k, d)
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            errs.append(f"{k} must be an integer >= 1")
            v = d
        ints[k] = v
    already = str_list(q.get("already_merged"))
    if already is None:
        errs.append("already_merged must be a list of unit ids")
        already = []
    auto = str_list(q.get("auto_merge"))
    if auto is None or any(a not in RISKS for a in auto):
        errs.append(f"auto_merge must be a list of {RISKS}")
        auto = []
    engine = q.get("engine") or "plain"
    if engine not in ENGINES:
        errs.append(f"engine must be one of {ENGINES}")
        engine = "plain"
    assume = q.get("assume_defaults", False)
    if not isinstance(assume, bool):
        errs.append("assume_defaults must be true or false")
        assume = False
    final = q.get("final")
    if final is not None:
        cmds = final.get("commands") if isinstance(final, dict) else None
        if not isinstance(cmds, dict) or not cmds or not all(isinstance(c, str) and c.strip() for c in cmds.values()):
            errs.append("final.commands must be a non-empty mapping name -> command")
            final = None
    shared = q.get("spec")
    shared_path = None
    if shared:
        shared_path = next((p for p in (repo / str(shared), qfile.parent / str(shared)) if p.exists()), None)
        if not shared_path:
            errs.append(f"spec '{shared}' not found (relative to the repo or the queue file)")
    units: list[dict] = []
    raw_units = q.get("units")
    if not isinstance(raw_units, list) or not raw_units:
        errs.append("units must be a non-empty list")
        raw_units = []
    seen: set[str] = set()
    for i, ru in enumerate(raw_units):
        if not isinstance(ru, dict):
            errs.append(f"units[{i}] must be a mapping")
            continue
        uid = str(ru.get("id") or "")
        tag = uid or f"units[{i}]"
        if not ID.fullmatch(uid):
            errs.append(f"{tag}: id must match [A-Za-z0-9_.-]+")
        if uid in seen:
            errs.append(f"duplicate unit id {uid}")
        seen.add(uid)
        risk = str(ru.get("risk") or "high")
        if risk not in RISKS:
            errs.append(f"{tag}: risk '{risk}' must be one of {RISKS}")
        lists = {k: str_list(ru.get(k)) for k in ("deps", "requirements", "files")}
        for k, v in lists.items():
            if v is None:
                errs.append(f"{tag}: {k} must be a list")
                lists[k] = []
        text = str(ru.get("spec") or "")
        if ru.get("spec_file"):
            sf = qfile.parent / str(ru["spec_file"])
            if sf.is_file():
                text = text or sf.read_text(encoding="utf-8")
            else:
                errs.append(f"{tag}: spec_file '{ru['spec_file']}' not found next to the queue file")
        elif not text.strip():
            errs.append(f"{tag}: no spec text (`spec` or `spec_file` required)")
        units.append({"id": uid, "title": str(ru.get("title") or uid), "deps": lists["deps"], "risk": risk,
                      "requirements": lists["requirements"], "files": [posix(f) for f in lists["files"] or []],
                      "spec": text})
    ids = {u["id"] for u in units}
    errs += [f"already_merged: unknown unit {x}" for x in already if x not in ids]
    deps = {u["id"]: u["deps"] for u in units}
    for u in units:
        for d in u["deps"]:
            if d not in ids:
                errs.append(f"{u['id']}: unknown dep {d}")
            elif d == u["id"]:
                errs.append(f"{u['id']}: depends on itself")
    cyc = [u["id"] for u in units if u["id"] in closure({k: [d for d in v if d in ids] for k, v in deps.items()},
                                                        u["id"])]
    if cyc:
        errs.append(f"dependency cycle through {', '.join(cyc)}")
    else:
        for i, a in enumerate(units):
            for b in units[i + 1:]:
                shared_files = sorted(set(a["files"]) & set(b["files"]))
                if shared_files and a["id"] not in closure(deps, b["id"]) and b["id"] not in closure(deps, a["id"]):
                    errs.append(f"{a['id']} and {b['id']} share {', '.join(shared_files)} but neither depends on "
                                f"the other")
    cfg = {"run_id": run_id, "base": str(q.get("base") or "HEAD"), "integration_branch": ib,
           "model": str(q.get("model") or "opus"), "writer": str(q.get("writer") or "sonnet"),
           "model_build": str(q["model_build"]) if q.get("model_build") else None,
           "model_fix": str(q["model_fix"]) if q.get("model_fix") else None, "unit_usd": unit_usd,
           "already_merged": already, "auto_merge": auto, "assume_defaults": assume, "engine": engine, "units": units,
           "final": final, "shared": shared_path, **nums, **ints}
    return cfg, errs


def git_problems(repo: Path, cfg: dict, run_dir: Path) -> list[str]:
    errs: list[str] = []
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")  # user's autocrlf: CRLF trees are clean
    if dirty.stdout.strip():
        errs.append("the working tree has uncommitted changes in tracked files")
    if git(repo, "rev-parse", "--verify", "-q", f"{cfg['base']}^{{commit}}").returncode != 0:
        errs.append(f"base '{cfg['base']}' does not exist")
    ib = cfg["integration_branch"]
    if git(repo, "check-ref-format", "--branch", ib).returncode != 0:
        errs.append(f"integration_branch '{ib}' is not a valid branch name")
    elif git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{ib}").returncode == 0:
        if not (run_dir / "state.json").is_file():
            errs.append(f"integration branch '{ib}' already exists and this run has no state")
        wts = git(repo, "worktree", "list", "--porcelain").stdout.splitlines()
        cur = ""
        for line in wts:
            if line.startswith("worktree "):
                cur = line[9:]
            elif line == f"branch refs/heads/{ib}" and not posix(cur).startswith(posix(run_dir)):
                errs.append(f"integration branch '{ib}' is checked out in {posix(cur)}")
    return errs


def order(cfg: dict, only: set[str] | None) -> list[dict]:
    todo = [u for u in cfg["units"] if only is None or u["id"] in only]
    out: list[dict] = []
    merged: set[str] = set()
    while True:
        nxt = next((u for u in todo if u not in out and all(d in merged for d in u["deps"])), None)
        if not nxt:
            return out
        out.append(nxt)
        merged.add(nxt["id"])


def caps(cfg: dict, n: int) -> tuple[float, float]:
    first_pass = cfg["max_usd"] * (1 - cfg["fix_reserve"])
    if cfg.get("unit_usd"):
        return first_pass, min(float(cfg["unit_usd"]), cfg["max_usd"])
    return first_pass, min(first_pass / max(n, 1) * 2, cfg["max_usd"])


def rate_slots(path: Path, n: int, now: float | None = None) -> tuple[int, float]:
    """(parallel slots, pause until epoch) from the latest rate-limit event the sessions saw."""
    now = time.time() if now is None else now
    try:
        r = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return n, 0.0
    if not isinstance(r, dict) or now - float(r.get("at") or 0) > RATE_FRESH_S:
        return n, 0.0
    if r.get("status") == "rejected" and float(r.get("resets_at") or 0) > now:
        return 0, float(r["resets_at"]) + 60
    util = float(r.get("utilization") or 0)
    if util >= 0.9 or r.get("status") == "allowed_warning":
        return 1, 0.0
    return (max(1, n // 2), 0.0) if util >= 0.75 else (n, 0.0)


class Queue:
    def __init__(self, cfg: dict, a: argparse.Namespace, repo: Path, qfile: Path):
        self.cfg, self.a, self.repo, self.qfile = cfg, a, repo, qfile
        self.only = set(a.only.split(",")) if a.only else None
        self.by_id = {u["id"]: u for u in cfg["units"]}
        self.run_dir = repo / ".powerpuff" / "runs" / cfg["run_id"]
        self.wt_root = self.run_dir / "wt"
        self.state_path = self.run_dir / "state.json"
        self.t0 = time.monotonic()
        self.outage = forge_run.Outage(getattr(a, "max_outage_min", 120))
        self.st: dict = {}
        self.selected = [u["id"] for u in cfg["units"] if self.only is None or u["id"] in self.only]
        self.lock = threading.RLock()
        self.rate_file = self.run_dir / "rate.json"
        self.pause_until = 0.0

    # ---- plumbing ---------------------------------------------------------------------------------
    def say(self, msg: str) -> None:
        with self.lock:
            print(f"{datetime.now():%H:%M:%S} queue: {msg}" if self.cfg.get("parallel", 1) > 1 else f"queue: {msg}",
                  flush=True)

    def save(self) -> None:
        with self.lock:
            self.st["elapsed_s"] = round(self.elapsed(), 1)
            for i in range(20):  # unit threads update their own entries without the lock
                try:
                    text = json.dumps(self.st, indent=1, ensure_ascii=False)
                    break
                except RuntimeError:
                    if i == 19:
                        raise
                    time.sleep(0.05)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.state_path)

    def elapsed(self) -> float:
        return float(self.st.get("elapsed_s_base", 0)) + time.monotonic() - self.t0

    def remaining_s(self) -> float:
        return self.cfg["max_hours"] * 3600 - self.elapsed()

    def u(self, uid: str) -> dict:
        return self.st["units"][uid]

    def spent_others(self, uid: str | None) -> float:
        return sum(float((v.get("cost") or {}).get("total_usd") or 0) + float(v.get("spent_before") or 0)
                   for k, v in self.st["units"].items() if k != uid)

    def suffix(self, uid: str) -> str:
        n = int(((self.st.get("units") or {}).get(uid) or {}).get("attempts") or 0)
        return f"-a{n + 1}" if n else ""

    def branch(self, uid: str) -> str:
        return f"powerpuff/{self.cfg['run_id']}/{uid}{self.suffix(uid)}"

    def wt_path(self, uid: str) -> Path:
        return self.wt_root / f"{uid}{self.suffix(uid)}"

    # ---- setup ------------------------------------------------------------------------------------
    def start(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        exclude = Path(self.repo, git(self.repo, "rev-parse", "--git-path", "info/exclude").stdout.strip())
        exclude.parent.mkdir(parents=True, exist_ok=True)
        cur = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        add = [e for e in EXCLUDES if e not in cur.splitlines()]
        if add:
            exclude.write_text(cur + ("" if cur.endswith("\n") or not cur else "\n") + "\n".join(add) + "\n",
                               encoding="utf-8")
        if self.state_path.is_file():
            self.st = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.st["elapsed_s_base"] = self.st.get("elapsed_s", 0)
        else:
            self.st = {"run_id": self.cfg["run_id"], "queue_file": posix(self.qfile), "created": now_iso(),
                       "base": self.cfg["base"], "elapsed_s": 0, "elapsed_s_base": 0, "units": {}, "final": None,
                       "base_sha": git(self.repo, "rev-parse", f"{self.cfg['base']}^{{commit}}").stdout.strip()}
        for u in self.cfg["units"]:
            self.st["units"].setdefault(u["id"], {"status": "pending", "branch": self.branch(u["id"]),
                                                  "worktree": None, "forge_status": None, "cost": None,
                                                  "started": None, "finished": None, "seconds": 0, "reason": ""})
        for uid in self.cfg.get("already_merged") or []:
            if self.st["units"][uid]["status"] != "merged":
                self.st["units"][uid].update(status="merged", reason="already merged into the base")
        self.st.setdefault("verified_trees", [])
        self.st["integration_branch"] = self.cfg["integration_branch"]
        ib = self.cfg["integration_branch"]
        if git(self.repo, "rev-parse", "--verify", "-q", f"refs/heads/{ib}").returncode != 0:
            git(self.repo, "branch", ib, self.st["base_sha"])
        for uid, v in self.st["units"].items():
            if v["status"] in ("blocked", "skipped-budget", "needs-answer"):
                v.update(status="pending", reason="")
            elif v["status"] == "done" and self.is_merged(uid):
                v.update(status="merged", reason="merged outside the queue")
        for uid in self.selected:
            v = self.u(uid)
            if v["status"] == "done" and self.by_id[uid]["risk"] in self.cfg["auto_merge"]:
                self.finish_ok(uid)
        self.save()

    def is_merged(self, uid: str) -> bool:
        return git(self.repo, "merge-base", "--is-ancestor", self.branch(uid),
                   self.cfg["integration_branch"]).returncode == 0

    def integration(self) -> Path:
        wt = self.wt_root / "_integration"
        with self.lock:
            return self._integration(wt)

    def _integration(self, wt: Path) -> Path:
        if not (wt / ".git").exists():
            git(self.repo, "worktree", "prune")
            r = git(self.repo, "worktree", "add", "--force", str(wt), self.cfg["integration_branch"])
            if r.returncode != 0:
                raise RuntimeError(f"cannot create the integration worktree: {r.stderr.strip()}")
        return wt

    def prepare_worktree(self, uid: str) -> Path:
        with self.lock:
            return self._prepare_worktree(uid)

    def _prepare_worktree(self, uid: str) -> Path:
        wt = self.wt_path(uid)
        if not (wt / ".git").exists():
            git(self.repo, "worktree", "prune")
            br = self.branch(uid)
            if git(self.repo, "rev-parse", "--verify", "-q", f"refs/heads/{br}").returncode == 0:
                r = git(self.repo, "worktree", "add", str(wt), br)
            else:
                r = git(self.repo, "worktree", "add", "-b", br, str(wt), self.cfg["integration_branch"])
            if r.returncode != 0:
                raise RuntimeError(f"cannot create the worktree of {uid}: {r.stderr.strip()}")
        return wt

    def write_spec(self, uid: str, wt: Path) -> Path:
        c = self.by_id[uid]
        parts = [f"# {uid}: {c['title']}", "",
                 f"Requirements: {', '.join(c['requirements']) or 'none listed'}",
                 f"Owned files: {', '.join(c['files']) or 'not restricted'}", "", "## Unit spec", "",
                 c["spec"].strip(), ""]
        prev = self.u(uid).get("history") or []
        if prev:
            last = prev[-1]
            parts += ["## Previous attempt (failed; do not repeat it)", "", f"- reason: {last.get('reason')}",
                      *([f"- disputes:\n{last['disputes']}"] if last.get("disputes") else []), ""]
        shared = self.cfg["shared"]
        if shared:
            parts.append(spec_slice.build(shared, c, self.repo))
        p = wt / ".forge-unit-spec.md"
        p.write_text("\n".join(parts), encoding="utf-8", newline="\n")
        return p

    # ---- budget -----------------------------------------------------------------------------------
    def budget_problem(self, uid: str) -> str:
        if self.remaining_s() <= 0:
            return "wall-clock budget exhausted"
        _, nominal = caps(self.cfg, len(self.selected))
        left = self.cfg["max_usd"] - self.spent_others(uid)
        if left < nominal - 1e-9:
            return f"remaining budget ${left:.2f} cannot cover the unit cap ${nominal:.2f}"
        return ""

    def unit_cap(self, uid: str) -> float:
        _, nominal = caps(self.cfg, len(self.selected))
        own = float(self.u(uid).get("spent_before") or 0)
        return max(0.01, min(self.cfg["max_usd"] - self.spent_others(uid) - own, nominal - own))

    # ---- scheduling -------------------------------------------------------------------------------
    def block_dependents(self) -> None:
        changed = True
        while changed:
            changed = False
            for uid in self.selected:
                v = self.u(uid)
                if v["status"] != "pending":
                    continue
                for d in self.by_id[uid]["deps"]:
                    ds = self.u(d)["status"]
                    why = ""
                    if ds in ("failed", "blocked", "skipped-budget", "needs-answer"):
                        why = f"dep {d} {ds}"
                    elif ds == "done":
                        why = f"awaits audit of {d}"
                    elif ds == "pending" and d not in self.selected:
                        why = f"dep {d} was not run (not selected)"
                    if why:
                        v.update(status="blocked", reason=why)
                        self.say(f"{uid} blocked: {why}")
                        changed = True
                        break
        self.save()

    def next_unit(self) -> str | None:
        for uid in self.selected:
            if self.u(uid)["status"] == "running":
                return uid
        for uid in self.selected:
            if self.u(uid)["status"] == "pending" \
                    and all(self.u(d)["status"] == "merged" for d in self.by_id[uid]["deps"]):
                return uid
        return None

    def skip_rest(self, why: str) -> None:
        for uid in self.selected:
            v = self.u(uid)
            if v["status"] in ("pending", "running"):
                v.update(status="skipped-budget", reason=why)
        self.say(f"remaining units skipped: {why}")
        self.save()

    # ---- running a unit ---------------------------------------------------------------------------
    def read_forge_state(self, wt: Path) -> dict:
        p = wt / ".forge" / "state.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def forge_cmd(self, spec: Path, wt: Path, cap: float) -> list[str]:
        hours = max(self.remaining_s(), 60) / 3600
        extra = [*(["--assume-defaults"] if self.cfg.get("assume_defaults") else []),
                 *(["--model-build", self.cfg["model_build"]] if self.cfg.get("model_build") else []),
                 *(["--model-fix", self.cfg["model_fix"]] if self.cfg.get("model_fix") else []),
                 *(["--skip-base-check"] if self.base_verified(wt) else [])]
        return [sys.executable, str(FORGE), str(spec), "--repo", str(wt), "--model", self.cfg["model"],
                "--writer", self.cfg["writer"], "--engine", self.cfg["engine"], "--max-usd", f"{cap:.4f}", "--max-hours", f"{hours:.4f}",
                "--no-wait", "--stream", *extra]

    # ---- base check cache: a tree whose gates were green once is not checked again ------------------
    def tree(self, wt: Path, rev: str) -> str:
        return git(wt, "rev-parse", f"{rev}^{{tree}}").stdout.strip()

    def base_verified(self, wt: Path) -> bool:
        fs = self.read_forge_state(wt)
        if fs.get("stage") not in (None, "init", "plain-tests", "audit") or "base_checked" in fs:
            return False
        rev = fs.get("start_sha") or "HEAD"
        return bool(self.tree(wt, rev)) and self.tree(wt, rev) in (self.st.get("verified_trees") or [])

    def note_verified(self, wt: Path) -> None:
        fs = self.read_forge_state(wt)
        trees = []
        if fs.get("base_checked") is True and fs.get("start_sha"):
            trees.append(self.tree(wt, fs["start_sha"]))
        if fs.get("status") == "done":
            trees.append(self.tree(wt, "HEAD"))
        with self.lock:
            known = self.st.setdefault("verified_trees", [])
            known += [t for t in trees if t and t not in known]
            del known[:-200]

    def run_forge(self, uid: str, wt: Path, spec: Path) -> tuple[int, str]:
        log = self.run_dir / f"{uid}.log"
        offset = log.stat().st_size if log.is_file() else 0
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
               "POWERPUFF_RATE_FILE": str(self.rate_file), **(PARALLEL_ENV if self.cfg.get("parallel", 1) > 1 else {})}
        t = time.monotonic()
        with open(log, "ab") as f:
            rc = subprocess.run(self.forge_cmd(spec, wt, self.unit_cap(uid)), cwd=wt, env=env, stdout=f,
                                stderr=subprocess.STDOUT).returncode
        self.u(uid)["seconds"] = round(self.u(uid).get("seconds", 0) + time.monotonic() - t, 1)
        with open(log, "rb") as f:
            f.seek(offset)
            out = f.read()[-20000:].decode("utf-8", errors="replace")
        return rc, out

    def sync_cost(self, uid: str, wt: Path) -> None:
        fs = self.read_forge_state(wt)
        v = self.u(uid)
        if fs.get("cost"):
            v["cost"] = {"total_usd": fs["cost"].get("total_usd", 0), "by_model": fs["cost"].get("by_model", {})}
        v["forge_status"] = fs.get("status") or fs.get("stage")

    def fail_reason(self, wt: Path, out: str, rc: int) -> str:
        rep = wt / ".forge" / "REPORT.md"
        if rep.is_file():
            first = rep.read_text(encoding="utf-8").splitlines()[:1]
            if first:
                return first[0].removeprefix("# FORGE REPORT - ").strip()
        lines = [x for x in out.splitlines() if "FORGE:" in x]
        return lines[-1].split("FORGE:", 1)[1].strip() if lines else f"forge exited with code {rc}"

    def run_unit(self, uid: str) -> str:
        """Returns ok (terminal status reached), rate (stop: rate limit with --no-wait) or budget."""
        v = self.u(uid)
        wt = self.prepare_worktree(uid)
        spec = self.write_spec(uid, wt)
        v.update(status="running", worktree=posix(wt), reason="")
        v["started"] = v["started"] or now_iso()
        self.save()
        self.say(f"{uid} running")
        crashes = 0
        while True:
            rc, out = self.run_forge(uid, wt, spec)
            self.sync_cost(uid, wt)
            self.note_verified(wt)
            self.save()
            if rc in (0, 1):
                self.outage.healthy()
            if rc == 0:
                v["finished"] = now_iso()
                self.snapshot(uid, wt)
                self.finish_ok(uid)
                why = "merge conflict with the integration branch"
                if v["status"] == "failed" and v["reason"] == "merge conflict" and self.retry(uid, wt, why):
                    wt, spec = self.next_attempt(uid, why)
                    crashes = 0
                    continue
                return "ok"
            if rc == 1:
                why = self.fail_reason(wt, out, rc)
                if "base is red" in why and self.wait_green_base(uid, wt, why):
                    wt = self.prepare_worktree(uid)
                    spec = self.write_spec(uid, wt)
                    crashes = 0
                    continue
                if "blocking questions" in why:
                    v.update(status="needs-answer", finished=now_iso(),
                             reason=f"{why}; answer in {posix(wt / '.forge' / 'questions.json')} and rerun")
                elif why.startswith("stuck:"):
                    disputes = wt / ".forge" / "disputes.md"
                    v.update(status="needs-answer", finished=now_iso(),
                             reason=f"{why}" + (f" | disputes: {' '.join(disputes.read_text(encoding='utf-8').split())[:400]}"
                                                if disputes.is_file() else ""))
                elif self.retry(uid, wt, why):
                    wt, spec = self.next_attempt(uid, why)
                    crashes = 0
                    continue
                else:
                    v.update(status="failed", reason=self.final_reason(uid, why), finished=now_iso())
                self.snapshot(uid, wt)
                self.say(f"{uid} {v['status']}: {v['reason']}")
                self.save()
                return "ok"
            if rc == 3 and forge_run.API_PAUSE in out:
                wait = None if self.a.no_wait else self.outage.next_wait(self.remaining_s())
                if wait is None:
                    v["reason"] = "API unreachable"
                    self.say(f"{uid} API unreachable, stopping")
                    self.save()
                    return "rate"
                self.say(f"{uid} API unreachable, retry in {wait:.0f} s (attempt {self.outage.attempt})")
                sleep(wait)
                continue
            if rc == 3:
                if self.a.no_wait:
                    v["reason"] = "rate limit"
                    self.say(f"{uid} rate limited, stopping (--no-wait)")
                    self.save()
                    return "rate"
                wait = min(forge_run.reset_wait_s(out), self.remaining_s())
                with self.lock:
                    self.pause_until = max(self.pause_until, time.time() + wait)
                if wait <= 0:
                    v.update(status="skipped-budget", reason="wall-clock budget exhausted while waiting for a rate limit")
                    self.save()
                    return "budget"
                self.say(f"{uid} rate limited, sleeping {wait / 60:.0f} min")
                sleep(wait)
                continue
            crashes += 1
            if crashes > 1:
                v.update(status="failed", reason=self.final_reason(uid, f"forge exited with code {rc} twice"),
                         finished=now_iso())
                self.snapshot(uid, wt)
                self.say(f"{uid} failed: {v['reason']}")
                self.save()
                return "ok"
            self.say(f"{uid} forge exited with code {rc}, retrying once")

    def next_attempt(self, uid: str, why: str) -> tuple[Path, Path]:
        v = self.u(uid)
        wt = self.prepare_worktree(uid)
        spec = self.write_spec(uid, wt)
        v.update(status="running", worktree=posix(wt), branch=self.branch(uid), reason="", finished=None)
        self.save()
        self.say(f"{uid} attempt {v['attempts'] + 1}/{self.cfg['max_attempts']} after: {why}")
        return wt, spec

    RED_BASE_WAITS = 3
    RED_BASE_WAIT_S = 900
    RED_BASE_POLL_S = 60

    def wait_green_base(self, uid: str, wt: Path, why: str) -> bool:
        """A red base reruns red on the same tree: wait for a new integration tip (or a flaky env to recover)
        and start again from a fresh worktree; nothing was spent, so no attempt is consumed."""
        v = self.u(uid)
        n = int(v.get("red_base_waits") or 0)
        if n >= self.RED_BASE_WAITS or self.remaining_s() <= 0:
            return False
        ib = self.cfg["integration_branch"]
        tip = git(self.repo, "rev-parse", ib).stdout.strip()
        v["red_base_waits"] = n + 1
        v.setdefault("history", []).append({"reason": why, "cost": 0.0, "disputes": ""})
        self.snapshot(uid, wt, f"{uid}.redbase{n + 1}")
        self.save()
        br = self.branch(uid)
        with self.lock:
            git(self.repo, "worktree", "remove", "--force", str(wt))
            shutil.rmtree(wt, ignore_errors=True)
            git(self.repo, "worktree", "prune")
            git(self.repo, "branch", "-D", br)
        self.say(f"{uid} {why}: waiting for a new integration tip ({n + 1}/{self.RED_BASE_WAITS})")
        waited = 0
        while waited < self.RED_BASE_WAIT_S and git(self.repo, "rev-parse", ib).stdout.strip() == tip:
            sleep(self.RED_BASE_POLL_S)
            waited += self.RED_BASE_POLL_S
        return True

    def final_reason(self, uid: str, why: str) -> str:
        n = int(self.u(uid).get("attempts") or 0) + 1
        return f"{why} (after {n} attempts; needs a human)" if n > 1 else why

    def retry(self, uid: str, wt: Path, why: str) -> bool:
        """Another attempt from a fresh branch when attempts are left: the failed one is archived with its reason."""
        v = self.u(uid)
        done = int(v.get("attempts") or 0) + 1
        if done >= self.cfg["max_attempts"] or self.remaining_s() <= 0:
            return False
        _, nominal = caps(self.cfg, len(self.selected))
        spent = float(v.get("spent_before") or 0) + float((v.get("cost") or {}).get("total_usd") or 0)
        if spent >= nominal - 0.01:
            return False
        disputes = wt / ".forge" / "disputes.md"
        hist = v.setdefault("history", [])
        hist.append({"reason": why, "cost": (v.get("cost") or {}).get("total_usd"),
                     "disputes": disputes.read_text(encoding="utf-8")[:3000] if disputes.is_file() else ""})
        v["attempts"] = done
        v["spent_before"] = round(float(v.get("spent_before") or 0) + float((v.get("cost") or {}).get("total_usd") or 0), 4)
        prev_models = v.setdefault("spent_before_by_model", {})
        for m, x in ((v.get("cost") or {}).get("by_model") or {}).items():
            prev_models[m] = round(float(prev_models.get(m, 0)) + float(x), 4)
        v["cost"] = None
        self.snapshot(uid, wt, f"{uid}.attempt{done}")
        self.save()  # the next attempt gets a new branch and worktree name (suffix -a<n>); the old ones go best-effort
        with self.lock:
            git(self.repo, "worktree", "remove", "--force", str(wt))
            shutil.rmtree(wt, ignore_errors=True)
            git(self.repo, "worktree", "prune")
        return True

    def finish_ok(self, uid: str) -> None:
        v = self.u(uid)
        v.update(status="done", reason="")
        if self.by_id[uid]["risk"] not in self.cfg["auto_merge"]:
            v["reason"] = "awaits audit"
            self.say(f"{uid} done, awaits audit")
            self.save()
            return
        if self.merge(uid):
            v["status"] = "merged"
            self.say(f"{uid} merged into {self.cfg['integration_branch']}")
            self.cleanup(uid)
        else:
            v.update(status="failed", reason="merge conflict")
            self.say(f"{uid} failed: merge conflict (branch {self.branch(uid)} kept)")
        self.save()

    def merge(self, uid: str) -> bool:
        with self.lock:
            wt = self.integration()
            r = git(wt, *IDENT, "merge", "--no-ff", "--no-gpg-sign", "-m",
                    f"powerpuff: merge {uid} ({self.by_id[uid]['title']})", self.branch(uid))
            if r.returncode != 0:
                git(wt, "merge", "--abort")
                return False
            return True

    def snapshot(self, uid: str, wt: Path, name: str | None = None) -> None:
        src = wt / ".forge"
        if src.is_dir():
            dst = self.run_dir / "forge" / (name or uid)
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.pyc", "__pycache__"))

    def cleanup(self, uid: str) -> None:
        wt = self.wt_path(uid)
        self.snapshot(uid, wt)
        with self.lock:
            git(self.repo, "worktree", "remove", "--force", str(wt))
            shutil.rmtree(wt, ignore_errors=True)
            git(self.repo, "worktree", "prune")

    # ---- final contour ----------------------------------------------------------------------------
    def final_commands(self) -> tuple[dict[str, str], str, str]:
        """(commands, source, profile error): profile tier `final` merged with queue.yaml `final.commands`, which win."""
        queue = (self.cfg["final"] or {}).get("commands") or {}
        prof, err = None, ""
        try:
            prof = forge_run.load_profile(self.repo)
        except ValueError as e:
            err = str(e)
        if not prof:
            return dict(queue), "queue" if queue else "none", err
        mine = prof["final_gates"]
        if not mine:
            return dict(queue), "queue" if queue else "none", err
        trust = "approved profile" if prof["approved"] else "profile NOT APPROVED"
        return {**mine, **queue}, f"{trust}{' + queue' if queue else ''}", err

    def run_final(self) -> None:
        cmds, source, err = self.final_commands()
        if not (cmds or err) or not any(self.u(x)["status"] == "merged" for x in self.selected):
            return
        head = git(self.repo, "rev-parse", self.cfg["integration_branch"]).stdout.strip()
        if (self.st.get("final") or {}).get("head") == head:
            return
        wt = self.integration()
        ctx = forge_check.gate_context(wt, self.st.get("base_sha"))
        env = forge_check.context_env(ctx)
        results = {}
        if err:
            results["profile"] = {"ok": False, "code": -2, "tail": err}
            self.say(f"final: profile FAIL ({err})")
        for name, cmd in cmds.items():
            self.say(f"final: {name}")
            try:
                p = subprocess.run(forge_check.expand(cmd, ctx), shell=True, cwd=wt, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", env=env,
                                   timeout=float((self.cfg["final"] or {}).get("timeout") or 1800))
                code, tail = p.returncode, (p.stdout + p.stderr).strip()[-2000:]
            except subprocess.TimeoutExpired:
                code, tail = -1, "timeout"
            results[name] = {"ok": code == 0, "code": code, "tail": tail}
            if code > 0 and forge_check.tool_missing(code, tail):
                results[name]["missing_tool"] = True
            self.say(f"final: {name} {'pass' if code == 0 else 'FAIL'}")
        self.st["final"] = {"head": head, "results": results, "source": source}
        self.save()

    # ---- results ----------------------------------------------------------------------------------
    def status(self) -> str:
        sts = [self.u(u["id"])["status"] for u in self.cfg["units"]]
        final_ok = all(r["ok"] for r in ((self.st.get("final") or {}).get("results") or {}).values())
        if not any(s in GOOD for s in sts):
            return "failed"
        return "done" if all(s in GOOD for s in sts) and final_ok else "partial"

    def forge_dir(self, uid: str) -> Path | None:
        wt = self.u(uid).get("worktree")
        for p in (Path(wt) / ".forge" if wt else None, self.run_dir / "forge" / uid):
            if p and p.is_dir():
                return p
        return None

    def unit_info(self, uid: str) -> dict:
        v, c = self.u(uid), self.by_id[uid]
        fd = self.forge_dir(uid)
        info = {"id": uid, "title": c["title"], "risk": c["risk"], "deps": c["deps"], **v,
                "minutes": round(v.get("seconds", 0) / 60, 1), "report": None, "decisions": [], "questions": [],
                "open_gaps": [], "guard": None}
        if not fd:
            return info
        rep = fd / "REPORT.md"
        info["report"] = posix(rep) if rep.is_file() else None
        req = (fd / "requirements.md").read_text(encoding="utf-8") if (fd / "requirements.md").is_file() else ""
        for key, prefix in (("decisions", "D"), ("questions", "Q")):
            info[key] = [x.strip() for x in req.splitlines() if re.match(rf"\s*[-*]?\s*\**{prefix}\d+", x)]
        fs = self.read_forge_state(fd.parent) if fd.name == ".forge" else {}
        if not fs and (fd / "state.json").is_file():
            fs = json.loads((fd / "state.json").read_text(encoding="utf-8"))
        info["open_gaps"] = fs.get("open_gaps") or []
        info["questions"] += [q for q in (fs.get("questions") or []) if isinstance(q, str)]
        try:
            info["guard"] = json.loads((fd / "check.json").read_text(encoding="utf-8")).get("guard")
        except (OSError, ValueError):
            pass
        return info

    def write_summary(self) -> None:
        infos = [self.unit_info(u["id"]) for u in self.cfg["units"]]
        by_model: dict[str, float] = {}
        total = 0.0
        for i in infos:  # failed earlier attempts (archived by retry) count too: they were paid for
            unit_usd = float((i.get("cost") or {}).get("total_usd") or 0) + float(i.get("spent_before") or 0)
            i["cost_all_attempts"] = round(unit_usd, 4)
            total += unit_usd
            for src in ((i.get("cost") or {}).get("by_model") or {}, i.get("spent_before_by_model") or {}):
                for m, x in src.items():
                    by_model[m] = round(by_model.get(m, 0) + float(x), 4)
        audit = [i["id"] for i in infos if i["status"] == "done"]
        blocked = [{"id": i["id"], "reason": i["reason"]} for i in infos if i["status"] == "blocked"]
        asks = [{"id": i["id"], "reason": i["reason"]} for i in infos if i["status"] == "needs-answer"]
        final = (self.st.get("final") or {}).get("results")
        data = {"run_id": self.cfg["run_id"], "status": self.status(), "base": self.cfg["base"],
                "integration_branch": self.cfg["integration_branch"], "elapsed_min": round(self.elapsed() / 60, 1),
                "cost": {"total_usd": round(total, 4), "by_model": by_model}, "units": infos,
                "awaiting_audit": audit, "needs_answer": asks, "blocked": blocked, "final": final,
                "final_gates_source": (self.st.get("final") or {}).get("source")}
        (self.run_dir / "summary.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        cell = lambda s: str(s).replace("|", "/").replace("\n", " ")  # noqa: E731
        lines = [f"# QUEUE SUMMARY - {data['status'].upper()} (run {self.cfg['run_id']})", "",
                 f"- integration branch: `{data['integration_branch']}` (base `{data['base']}`)",
                 f"- cost (notional USD): {data['cost']['total_usd']} by model {by_model}",
                 f"- elapsed: {data['elapsed_min']} min", "",
                 "| unit | status | risk | cost | min | branch | reason |", "|---|---|---|---|---|---|---|"]
        for i in infos:
            lines.append(f"| {i['id']} | {i['status']} | {i['risk']} | {i['cost_all_attempts']} | "
                         f"{i['minutes']} | `{i['branch']}` | {cell(i['reason'])} |")
        lines += ["", "## Awaiting audit", *([f"- {x}: `{self.u(x)['branch']}`" for x in audit] or ["none"]), "",
                  "## Needs answer", *([f"- {b['id']}: {b['reason']}" for b in asks] or ["none"]), "",
                  "## Blocked", *([f"- {b['id']}: {b['reason']}" for b in blocked] or ["none"]), ""]
        if final:
            lines += ["## Final contour", f"- gates source: {data['final_gates_source']}"]
            for name, r in final.items():
                lines += [f"- {name}: {'pass' if r['ok'] else 'FAIL'} (exit {r['code']})", "  ```", *(
                    "  " + x for x in r["tail"].splitlines()[-15:]), "  ```"]
            lines.append("")
        (self.run_dir / "SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")

    # ---- main loop --------------------------------------------------------------------------------
    def loop(self) -> int | None:
        while True:
            self.block_dependents()
            uid = self.next_unit()
            if not uid:
                return None
            why = self.budget_problem(uid)
            if why:
                self.skip_rest(why)
                return None
            self.integration()
            outcome = self.run_unit(uid)
            if outcome == "rate":
                return 3
            if outcome == "budget":
                self.skip_rest("wall-clock budget exhausted")
                return None
            self.block_dependents()
            if self.a.attended and self.next_unit():
                print(f"QUEUE: paused after {uid}", flush=True)
                return 4

    def ready(self, busy: set[str]) -> list[str]:
        """Units to start now: interrupted `running` ones first, then pending units whose deps are merged."""
        run = [u for u in self.selected if u not in busy and self.u(u)["status"] == "running"]
        new = [u for u in self.selected if u not in busy and self.u(u)["status"] == "pending"
               and all(self.u(d)["status"] == "merged" for d in self.by_id[u]["deps"])]
        return run + new

    def loop_parallel(self, n: int, tick: float = 10.0, stagger: float = 5.0) -> int | None:
        threads: dict[str, threading.Thread] = {}
        results: dict[str, str] = {}
        stop: str | None = None

        def work(uid: str) -> None:
            try:
                results[uid] = self.run_unit(uid)
            except Exception as e:  # noqa: BLE001 - one broken unit never takes the others down
                with self.lock:
                    merged = self.is_merged(uid)
                    self.u(uid).update(status="merged" if merged else "failed", finished=now_iso(),
                                       reason=f"queue error after the merge: {e}" if merged else f"queue error: {e}")
                    self.save()
                results[uid] = "ok"
        while True:
            for uid, t in list(threads.items()):
                if not t.is_alive():
                    t.join()
                    del threads[uid]
                    out = results.pop(uid, "ok")
                    stop = stop or (out if out in ("rate", "budget") else None)
            with self.lock:
                self.block_dependents()
            slots, until = rate_slots(self.rate_file, n)
            pause = max(until, self.pause_until) > time.time()
            if stop is None and not pause:
                for uid in self.ready(set(threads))[:max(0, slots - len(threads))]:
                    why = self.budget_problem(uid)
                    if why:
                        stop = "budget"
                        self.say(f"not starting {uid}: {why}")
                        break
                    self.integration()
                    threads[uid] = threading.Thread(target=work, args=(uid,), daemon=True, name=f"unit-{uid}")
                    threads[uid].start()
                    sleep(stagger)
            if not threads:
                if stop == "rate":
                    return 3
                if stop == "budget":
                    self.skip_rest("budget exhausted")
                    return None
                if not pause and not self.ready(set()):
                    return None
            sleep(tick)

    def run(self) -> int:
        self.start()
        code: int | None = None
        try:
            n = self.cfg.get("parallel", 1)
            code = self.loop_parallel(n) if n > 1 and not self.a.attended else self.loop()
            if code is None:
                self.run_final()
                wt = self.wt_root / "_integration"
                if (wt / ".git").exists():
                    git(self.repo, "worktree", "remove", "--force", str(wt))
                    git(self.repo, "worktree", "prune")
        finally:
            self.save()
            self.write_summary()
        if code is not None:
            return code
        status = self.status()
        print(f"QUEUE: {status}", flush=True)
        return {"done": 0, "partial": 2, "failed": 1}[status]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("queue")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument("--max-outage-min", type=float, default=120, help="total wait for an unreachable API before stopping")
    ap.add_argument("--engine", choices=("plain", "forge"), help="overrides `engine` of the queue file (default plain)")
    ap.add_argument("--parallel", type=int, default=0, help="independent units at once (overrides queue.yaml `parallel`)")
    ap.add_argument("--only", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--attended", action="store_true")
    a = ap.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    def config_error(errs: list[str]) -> int:
        print("QUEUE: config error")
        for e in errs:
            print(f"- {e}")
        return 3

    qfile = Path(a.queue).resolve()
    top = git(Path(a.repo).resolve() if Path(a.repo).is_dir() else ".", "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return config_error([f"{a.repo} is not a git repository"])
    repo = Path(top.stdout.strip())
    try:
        cfg, errs = normalize(load_queue(qfile), qfile, repo)
    except (OSError, ValueError) as e:
        return config_error([f"cannot read {qfile}: {e}"])
    if a.engine:
        cfg["engine"] = a.engine
    if a.parallel:
        cfg["parallel"] = max(1, a.parallel)
    if a.only:
        errs += [f"--only: unknown unit {x}" for x in a.only.split(",") if x not in {u["id"] for u in cfg["units"]}]
    run_dir = repo / ".powerpuff" / "runs" / cfg["run_id"]
    errs += git_problems(repo, cfg, run_dir)
    if errs:
        return config_error(errs)
    q = Queue(cfg, a, repo, qfile)
    if a.dry_run:
        plan = order(cfg, q.only)
        first_pass, cap = caps(cfg, len(q.selected))
        print(f"queue: dry-run run_id={cfg['run_id']} base={cfg['base']} integration={cfg['integration_branch']}")
        print(f"queue: parallel={cfg['parallel']} max_attempts={cfg['max_attempts']} model={cfg['model']} "
              f"model_build={cfg['model_build'] or cfg['model']} model_fix={cfg['model_fix'] or cfg['model_build'] or cfg['model']}")
        print(f"queue: budget max_usd={cfg['max_usd']} max_hours={cfg['max_hours']} fix_reserve={cfg['fix_reserve']} "
              f"first_pass=${first_pass:.2f} reserve=${cfg['max_usd'] - first_pass:.2f} unit_cap=${cap:.2f}")
        for n, u in enumerate(plan, 1):
            merge = "auto-merge" if u["risk"] in cfg["auto_merge"] else "awaits audit"
            print(f"queue: {n}. {u['id']} risk={u['risk']} ({merge}) deps={','.join(u['deps']) or '-'}")
        print("QUEUE: dry-run ok")
        return 0
    return q.run()


if __name__ == "__main__":
    sys.exit(main())
