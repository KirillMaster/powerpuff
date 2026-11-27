"""prepare - spec preparation with the human present: spec (dir | file | idea text) -> queue.yaml for queue_run.py.
The orchestrator is this script; the model only proposes (every session is read-only, the script writes all files).

Stages (state in <repo>/.powerpuff/prepare/<slug>/state.json, resumable per stage):
  lint       spec dir -> spec_lint; errors stop the run (exit 1) unless --accept-lint; idea/file -> skipped
  base       powerpuff.yaml gates on the untouched tree; red base stops the run before any model cost
  questions  ONE session audits the spec against the repository -> Q/D lines -> questions.json;
             unanswered `blocking` questions -> exit 4 (answer in questions.json / --answers / --assume-defaults)
  slice      ONE session drafts units.yaml; the script validates it deterministically, <=2 repair sessions
  emit       --out queue.yaml, accepted by queue_run's validator (dry-run), last line `PREPARE: ready <path>`

Usage: python prepare_run.py <spec-dir|spec-file|"idea text"> [--repo .] [--model opus] [--out queue.yaml]
       [--max-unit-lines 200] [--max-usd 5] [--no-wait] [--answers questions.json] [--assume-defaults]
       [--accept-lint] [--skip-base-check] [--dry-run]
       [--max-outage-min 120]
Exit: 0 ready, 1 failed, 3 rate limit with --no-wait or API unreachable (backoff 30 s .. 15 min, up to
--max-outage-min in total; resumable), 4 needs answers.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_check  # noqa: E402
import forge_run  # noqa: E402
import queue_run  # noqa: E402
import spec_lint  # noqa: E402

try:
    import yaml
except ImportError:
    yaml = None

REQ_REGEX = r"\b(?:FR|NFR|AS|US)-\d+\b|\bR\d+\b"
D_LINE = re.compile(r"^\s*[-*]?\s*\**D(\d+)\b")
FENCE = re.compile(r"```[ \t]*(?:ya?ml|json)?[ \t]*\n(.*?)```", re.DOTALL | re.IGNORECASE)
MAX_REPAIRS = 2
SPEC_ONLY = ("spec.yaml", "spec.md")
READ_ONLY_TOOLS = "Read,Grep,Glob"

QUESTIONS = """You are the spec auditor of a prepared run. Read-only: do NOT edit or create any file.
Read `SPEC` (the full spec). Explore the repository only as far as needed (entry points, the modules the spec touches,
existing tests, conventions). Hunt for ambiguities where a wrong guess costs a rework: public API shape, data model and
migrations, order of checks, boundary inclusivity, rounding, what resets vs carries over, external side effects,
existing behaviour that must keep working, things the spec leaves to "obvious" defaults.
Answer in your final message with lines only, exactly these forms (one line each):
- `Q<n> [blocking|non-blocking|info]: <question> | default: <the chosen default> | cost of a wrong guess: <one phrase>`
  `blocking` ONLY when a wrong guess would make a whole unit worthless or irreversible (data loss, external side
  effects, public API shape); everything else gets a default and is `non-blocking`; `info` = a noteworthy assumption
  that needs no answer.
- `D<n>: <ambiguity> -> <chosen reading> - <basis>` for every default you take (basis: existing code, literal wording,
  consistency with the spec, the stricter reading).
Finish with one line: QUESTIONS: done"""

SLICE_RULES = """Rules:
- Each unit is independently testable and mergeable; target diff <= MAXLINES lines (`est_lines` = your honest estimate).
- Vertical slices (one behaviour end to end) over horizontal layers.
- `files` = every file the unit will create or modify (repo-relative, forward slashes). Two units that share a file
  need a dependency between them. A file's parent directory must exist in the repository or be created by the unit
  itself or by a unit it depends on.
- `risk: high` for public API, data migration, security, external side effects, and anything a blocking question
  touched; otherwise `low`.
- Every requirement id below is assigned to at least one unit (`requirements`):
IDS
- Human decisions below are binding: apply them, do not re-ask.
Output: ONE fenced yaml block and nothing after it, a list of units:
```yaml
- id: U1
  title: <short>
  deps: []
  risk: low
  requirements: [FR-1]
  files: [src/x.py]
  est_lines: 80
  spec: |
    What to build, the observable behaviour, the checks (what the unit's tests must assert).
```
Unit ids are unique `[A-Za-z0-9_.-]+`; `deps` are ids of other units; `spec` is mandatory and self-contained."""

SLICE = """You slice a spec into work units for an autonomous run. Read-only: do NOT edit or create any file.
Read `SPEC` (the full spec) and explore the repository only as far as needed to know which files exist.
""" + SLICE_RULES

REPAIR = """The unit draft failed validation. Read-only: do NOT edit or create any file.
Fix EVERY problem below and return the complete corrected list of units (same output format). Keep what is valid.
The spec is `SPEC`.

Problems:
PROBLEMS

""" + SLICE_RULES + """

Current draft:
DRAFT"""


class Stop(Exception):
    def __init__(self, rc: int):
        super().__init__(rc)
        self.rc = rc


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "spec"


def posix(p: Path | str) -> str:
    return str(p).replace(chr(92), "/")


def dump_yaml(data: object) -> str:
    class D(yaml.SafeDumper):  # type: ignore[name-defined, misc]
        pass

    D.add_representer(str, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None))
    return yaml.dump(data, Dumper=D, sort_keys=False, allow_unicode=True, width=110)  # type: ignore[union-attr]


def parse_units(text: str) -> tuple[list | None, str]:
    """The units list from a model answer (first fenced block, else the whole text); (None, why) when unusable."""
    m = FENCE.search(text)
    body = m.group(1) if m else text
    try:
        data = yaml.safe_load(body) if yaml else json.loads(body)
    except Exception as e:  # noqa: BLE001 - any parse failure is a problem for the repair round, not a crash
        return None, f"the answer is not parseable as {'YAML' if yaml else 'JSON'}: {str(e)[:200]}"
    if isinstance(data, dict) and isinstance(data.get("units"), list):
        data = data["units"]
    if not isinstance(data, list):
        return None, "the answer is not a list of units"
    return data, ""


class Prepare:
    def __init__(self, a: argparse.Namespace, root: Path):
        self.a, self.root = a, root
        self.persist = not a.dry_run
        src = Path(a.spec) if a.spec.strip() and len(a.spec) < 260 and "\n" not in a.spec else None
        self.src = src.resolve() if src and src.exists() else None
        if self.src:
            self.kind = "dir" if self.src.is_dir() else "file"
            self.slug = slugify(self.src.name if self.kind == "dir" else self.src.stem)
        else:
            self.kind = "idea"
            self.slug = "idea-" + hashlib.sha1(a.spec.encode("utf-8")).hexdigest()[:8]
        self.dir = root / ".powerpuff" / "prepare" / self.slug
        self.state_path = self.dir / "state.json"
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.is_file() else {}
        self.state.setdefault("cost", {"total_usd": 0.0, "by_model": {}, "by_stage": {}, "sessions": 0, "turns": 0})
        self.state.setdefault("slug", self.slug)
        self.outage = forge_run.Outage(getattr(a, "max_outage_min", 120))

    # ---- plumbing ---------------------------------------------------------------------------------
    def log(self, msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        if self.persist and self.dir.is_dir():
            with open(self.dir / "progress.md", "a", encoding="utf-8") as f:
                f.write("- " + line + "\n")
        print(line, flush=True)

    def save(self) -> None:
        if self.persist:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.state, indent=1, ensure_ascii=False), encoding="utf-8")

    def fail(self, why: str, lines: list[str] | None = None) -> Stop:
        for x in lines or []:
            print(x)
        print(f"PREPARE: failed ({why})", flush=True)
        return Stop(1)

    def git(self, *args: str) -> str:
        p = subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=self.root, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return p.stdout.strip()

    def rel(self, p: Path) -> str:
        try:
            return posix(p.resolve().relative_to(self.root))
        except ValueError:
            return posix(p.resolve())

    def spec_text(self) -> str:
        if self.kind == "dir":
            assert self.src
            parts = [f"# {n}\n\n{(self.src / n).read_text(encoding='utf-8')}" for n in forge_run.SPEC_FILES
                     if (self.src / n).is_file()]
            return "\n\n".join(parts)
        if self.kind == "file":
            assert self.src
            return self.src.read_text(encoding="utf-8")
        return self.a.spec

    def req_ids(self) -> list[str]:
        if self.kind == "dir":
            assert self.src
            text = "\n".join((self.src / n).read_text(encoding="utf-8") for n in SPEC_ONLY if (self.src / n).is_file())
        else:
            text = self.spec_text()
        return list(dict.fromkeys(re.findall(self.a.req_regex, text)))

    # ---- model sessions ---------------------------------------------------------------------------
    def session(self, stage: str, prompt: str) -> dict:
        """One read-only `claude -p` session. Returns {"ok", "text"}; raises forge_run.RateLimited."""
        if self.state["cost"]["total_usd"] >= self.a.max_usd:
            raise self.fail(f"cost budget exhausted: ${self.state['cost']['total_usd']:.2f} of ${self.a.max_usd:g}")
        model = self.a.model
        argv = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", model,
                "--allowedTools", READ_ONLY_TOOLS]
        advisor = getattr(self.a, "advisor", None)
        advisor = advisor if advisor is not None else os.environ.get("POWERPUFF_ADVISOR")
        adv = forge_run.advisor_settings(advisor, self.dir)
        argv += adv
        prompt += forge_run.ADVISOR_NOTE if adv else ""
        tally: dict = {}
        self.log(f"{stage}: session start ({model}{f', advisor {advisor}' if adv else ''})")
        proc = subprocess.Popen(argv, cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                shell=sys.platform == "win32", env={**os.environ, "POWERPUFF_DRIVER": "1"})
        assert proc.stdin and proc.stdout
        proc.stdin.write(prompt)
        proc.stdin.close()
        result, tail, api_msg = None, [], False
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                tail = (tail + [line.strip()])[-20:]
                continue
            forge_run.advisor_event(ev, tally)
            if isinstance(ev, dict) and ev.get("type") == "assistant" and ev.get("is_api_error_message"):
                api_msg = True
            if isinstance(ev, dict) and ev.get("type") == "result":
                result = ev
        proc.wait()
        if tally.get("errors"):
            self.log(f"{stage}: WARNING advisor unavailable ({tally.get('last_error')}), continuing without it")
        if adv:
            self.log(f"{stage}: advisor_calls={tally.get('calls', 0)}")
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
        kind = forge_run.failure_kind(result, text, tail, api_msg)
        if kind == "rate":
            raise forge_run.RateLimited(text + "\n" + "\n".join(tail))
        if kind == "api":
            raise forge_run.ApiUnreachable(text + "\n" + "\n".join(tail))
        self.outage.healthy()
        ok = bool(result) and not r.get("is_error")
        self.log(f"{stage}: session end ok={ok} cost={spent:.2f}"
                 + ("" if ok else " error=" + " ".join(text.split())[:200] + " tail=" + " | ".join(tail[-3:])[:300]))
        return {"ok": ok, "text": text}

    def staged(self, stage: str, prompt: str, tries: int = 2) -> str:
        """Runs a session until it ends ok, sleeping through rate limits; failure after `tries` -> exit 1."""
        left = tries
        while left > 0:
            try:
                out = self.session(stage, prompt)
            except forge_run.RateLimited as e:
                if self.a.no_wait:
                    print("PREPARE: rate limited (rerun to resume)", flush=True)
                    raise Stop(3) from None
                wait = forge_run.reset_wait_s(e.text)
                self.log(f"{stage}: rate limited, sleeping {wait / 60:.0f} min")
                time.sleep(wait)
                continue  # a rate limit does not consume a try
            except forge_run.ApiUnreachable:
                wait = None if self.a.no_wait else self.outage.next_wait()
                if wait is None:
                    print("PREPARE: paused (API unreachable, rerun to resume)", flush=True)
                    raise Stop(3) from None
                self.log(f"{stage}: API unreachable, retry in {wait:.0f} s (attempt {self.outage.attempt})")
                time.sleep(wait)
                continue  # an API outage does not consume a try either
            left -= 1
            if out["ok"]:
                return out["text"]
        raise self.fail(f"{stage} session failed")

    # ---- stage 1: lint ----------------------------------------------------------------------------
    def stage_lint(self) -> None:
        if self.state.get("lint"):
            return
        if self.kind != "dir":
            self.log(f"lint: skipped ({self.kind} input is not a spec dir)")
            self.state["lint"] = "skipped"
            self.save()
            return
        assert self.src
        try:
            res = spec_lint.lint(self.src, self.root)
        except spec_lint.InputError as e:
            raise self.fail(f"lint: {e}") from None
        if self.persist:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / "lint.json").write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
        found = [spec_lint.format_finding(f) for f in res["errors"]]
        self.log(f"lint: {len(res['errors'])} error(s), {len(res['warnings'])} warning(s)")
        if found and not self.a.accept_lint:
            raise self.fail(f"spec lint: {len(found)} error(s), see {self.rel(self.dir / 'lint.json')}; "
                            f"fix the spec or pass --accept-lint", found)
        self.state["lint"] = "accepted" if found else "ok"
        self.state["lint_accepted"] = found
        self.save()

    # ---- stage 2: base ----------------------------------------------------------------------------
    def stage_base(self) -> None:
        if self.state.get("base"):
            return
        if self.a.skip_base_check:
            self.state["base"] = "skipped"
            self.save()
            return
        try:
            prof = forge_run.load_profile(self.root)
        except ValueError as e:
            raise self.fail(f"profile: {e}") from None
        if not prof or not prof["approved"]:
            print("gates: no approved profile" + (" (powerpuff.yaml has approved: false)" if prof else ""), flush=True)
        if not prof:
            self.state["base"] = "no-profile"
            self.save()
            return
        res = forge_check.check_gates(self.root, self.a.gate_timeout, prof["gates"],
                                      forge_check.gate_context(self.root, self.git("rev-parse", "HEAD")))["results"]
        for name, r in res.items():
            if r.get("missing_tool"):
                raise self.fail(f"base check: missing tool in gate '{name}'", [f"- {name}: {r.get('tail', '')}"])
        red = {n: r.get("tail", "") for n, r in res.items() if not r["ok"]}
        if red:
            raise self.fail(f"base is red: {', '.join(red)}",
                            [f"- {n}: {' '.join(str(t).split())[-400:]}" for n, t in red.items()])
        self.log(f"base: green ({', '.join(res)})")
        self.state["base"] = "green"
        self.state["profile_final"] = {g: prof["gates"].get(g) or prof["config"]["commands"].get(g)
                                       for g in prof["final"]}
        self.save()

    # ---- stage 3: questions -----------------------------------------------------------------------
    @property
    def qpath(self) -> Path:
        return self.dir / "questions.json"

    def load_questions(self) -> list[dict]:
        try:
            data = json.loads(self.qpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise self.fail(f"{self.rel(self.qpath)} is not valid JSON") from None
        if not isinstance(data, list) or not all(isinstance(q, dict) for q in data):
            raise self.fail(f"{self.rel(self.qpath)} must be a JSON list of questions")
        return data

    def merge_answers(self, qs: list[dict]) -> None:
        if not self.a.answers:
            return
        try:
            given = json.loads(Path(self.a.answers).read_text(encoding="utf-8"))
            assert isinstance(given, list)
        except (OSError, ValueError, AssertionError):
            raise self.fail(f"--answers {self.a.answers} is not a JSON list of questions") from None
        by_id = {str(g.get("id")): g.get("answer") for g in given if isinstance(g, dict)}
        for q in qs:
            if forge_run.answered({"answer": by_id.get(str(q.get("id")))}):
                q["answer"] = by_id[str(q.get("id"))]
        self.qpath.write_text(json.dumps(qs, indent=1, ensure_ascii=False), encoding="utf-8")

    def stage_questions(self) -> None:
        if not self.state.get("questions_done"):
            text = self.staged("questions", QUESTIONS.replace("SPEC", self.rel(self.dir / "spec.md")))
            lines = text.splitlines()
            qs = [forge_run.parse_question(x) for x in lines if forge_run.Q_LINE.match(x)]
            decisions = [x.strip() for x in lines if D_LINE.match(x)]
            self.qpath.write_text(json.dumps(qs, indent=1, ensure_ascii=False), encoding="utf-8")
            (self.dir / "decisions.md").write_text("\n".join(decisions) + "\n", encoding="utf-8")
            self.state["questions_done"] = True
            self.save()
            self.log(f"questions: {len(qs)} question(s), {len(decisions)} default decision(s)")
        qs = self.load_questions()
        self.merge_answers(qs)
        open_q = [q for q in qs if q.get("class") == "blocking" and not forge_run.answered(q)]
        if open_q and not self.a.assume_defaults:
            print("| # | id | question | default | cost of a wrong guess |\n|---|---|---|---|---|")
            for n, q in enumerate(open_q, 1):
                print(f"| {n} | {q.get('id')} | {forge_run.question_text(q)} | {q.get('default')} | "
                      f"{q.get('wrong_guess_cost')} |")
            print("answer: fill `answer` in questions.json (or --answers <file>, or --assume-defaults) and rerun",
                  flush=True)
            print(f"PREPARE: needs answers {self.rel(self.qpath)}", flush=True)
            raise Stop(4)
        self.state["assumed_defaults"] = [str(q.get("id")) for q in open_q]
        self.save()

    def decisions_text(self) -> str:
        qs = self.load_questions()
        assumed = set(self.state.get("assumed_defaults") or [])
        out: list[str] = []
        ans = [q for q in qs if forge_run.answered(q)]
        if ans:
            out += ["Human decisions (binding):"] + [
                f"- {q.get('id')}: {forge_run.question_text(q)} => {str(q['answer']).strip()}" for q in ans]
        dflt = [q for q in qs if not forge_run.answered(q) and q.get("class") in ("blocking", "non-blocking")]
        if dflt:
            out += ["Defaults taken (no human answer; a `blocking` one touches its units: risk high):"] + [
                f"- {q.get('id')}{' [blocking, assumed]' if q.get('id') in assumed else ''}: "
                f"{forge_run.question_text(q)} => {q.get('default')}" for q in dflt]
        d_path = self.dir / "decisions.md"
        decisions = d_path.read_text(encoding="utf-8").strip() if d_path.is_file() else ""
        if decisions:
            out += ["Default decisions of the audit:", decisions]
        return "\n".join(out) or "(none)"

    # ---- stage 4: slice ---------------------------------------------------------------------------
    def spec_ref(self) -> str:
        return self.rel(self.src) if self.src else self.rel(self.dir / "spec.md")

    def validate(self, units: object) -> list[str]:
        if not isinstance(units, list) or not units:
            return ["units: must be a non-empty list"]
        problems: list[str] = []
        cand = self.dir / "candidate.queue.json"
        cand.write_text(json.dumps({"run_id": "validate", "spec": self.spec_ref(), "units": units}, default=str),
                        encoding="utf-8")
        cfg, errs = queue_run.normalize(queue_run.load_queue(cand), cand, self.root)
        problems += errs
        dicts = [u for u in units if isinstance(u, dict)]
        for u in dicts:
            tag = str(u.get("id") or "?")
            if "risk" not in u:
                problems.append(f"{tag}: risk is missing (low or high)")
            est = u.get("est_lines")
            if isinstance(est, bool) or not isinstance(est, (int, float)):
                problems.append(f"{tag}: est_lines is missing or not a number")
            elif est > self.a.max_unit_lines:
                problems.append(f"{tag}: est_lines {est:g} exceeds --max-unit-lines {self.a.max_unit_lines} "
                                f"(split the unit)")
        covered = {str(r) for u in cfg["units"] for r in u["requirements"]}
        for rid in self.req_ids():
            if rid not in covered:
                problems.append(f"requirement {rid} is not assigned to any unit")
        deps = {u["id"]: u["deps"] for u in cfg["units"]}
        for u in cfg["units"]:
            chain = {u["id"]} | queue_run.closure(deps, u["id"])
            made = [f for x in cfg["units"] if x["id"] in chain for f in x["files"]]
            for f in u["files"]:
                d = f.rsplit("/", 1)[0] if "/" in f else ""
                if d and not (self.root / d).is_dir() and not any(m.startswith(d + "/") and m != f for m in made):
                    problems.append(f"{u['id']}: directory {d}/ of {f} does not exist and no earlier unit in its "
                                    f"dependency chain creates it")
        return list(dict.fromkeys(problems))

    def write_draft(self, units: list) -> None:
        body = dump_yaml(units) if yaml else json.dumps(units, indent=1, ensure_ascii=False)
        (self.dir / "units.yaml").write_text(body, encoding="utf-8")

    def stage_slice(self) -> None:
        draft_path = self.dir / "units.yaml"
        spec = self.rel(self.dir / "spec.md")
        rules = {"MAXLINES": str(self.a.max_unit_lines),
                 "IDS": "  " + (", ".join(self.req_ids()) or "(the spec has no ids: assign what you derive)")}

        def fill(t: str) -> str:
            for k, v in rules.items():
                t = t.replace(k, v)
            return t.replace("SPEC", spec)

        if not self.state.get("slice_done_first"):
            text = self.staged("slice", fill(SLICE) + "\n\n" + self.decisions_text())
            (self.dir / "slice.raw.txt").write_text(text, encoding="utf-8")
            units, why = parse_units(text)
            if units is not None:
                self.write_draft(units)
            else:
                draft_path.unlink(missing_ok=True)
                self.state["draft_error"] = why
            self.state["slice_done_first"] = True
            self.save()
        for rnd in range(int(self.state.get("repairs", 0)), MAX_REPAIRS + 1):
            if draft_path.is_file():
                units, _ = parse_units(draft_path.read_text(encoding="utf-8"))
                problems = self.validate(units)
            else:
                units, problems = None, [str(self.state.get("draft_error") or "no parseable draft")]
            if not problems:
                self.state["units"] = units
                self.save()
                return
            if rnd == MAX_REPAIRS:
                raise self.fail(f"draft still invalid after {MAX_REPAIRS} repair rounds ({self.rel(draft_path)})",
                                [f"- {p}" for p in problems])
            self.log(f"slice: {len(problems)} problem(s), repair round {rnd + 1}")
            draft = draft_path.read_text(encoding="utf-8") if draft_path.is_file() else \
                (self.dir / "slice.raw.txt").read_text(encoding="utf-8")[-6000:]
            text = self.staged(f"repair{rnd + 1}", fill(REPAIR).replace("PROBLEMS", "\n".join(f"- {p}" for p in problems))
                               .replace("DRAFT", draft) + "\n\n" + self.decisions_text())
            fixed, why = parse_units(text)
            if fixed is not None:
                self.write_draft(fixed)
            else:
                self.state["draft_error"] = why
            self.state["repairs"] = rnd + 1
            self.save()

    # ---- stage 5: emit ----------------------------------------------------------------------------
    def stage_emit(self) -> Path:
        units: list[dict] = self.state["units"]
        qs = self.load_questions()
        out = Path(self.a.out).resolve()
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
        low = sum(1 for u in units if u["risk"] == "low")
        ids = self.req_ids()
        covered = {str(r) for u in units for r in u.get("requirements") or []}
        n_cov = sum(1 for i in ids if i in covered)
        answered = sum(1 for q in qs if forge_run.answered(q))
        defaulted = sum(1 for q in qs if not forge_run.answered(q) and q.get("class") in ("blocking", "non-blocking"))
        queue: dict = {"run_id": f"{datetime.now():%Y-%m-%d}-{self.slug}"[:60].rstrip("-"), "base": branch,
                       "spec": self.spec_ref(),
                       "budget": {"max_usd": self.a.queue_max_usd, "max_hours": self.a.queue_max_hours,
                                  "fix_reserve": 0.33},
                       "model": self.a.model, "auto_merge": ["low"]}
        advisor = getattr(self.a, "advisor", None)
        if not forge_run.advisor_off(advisor):
            queue["advisor"] = advisor
        final = {k: v for k, v in (self.state.get("profile_final") or {}).items() if v}
        if final:
            queue["final"] = {"timeout": 1800, "commands": final}
        queue["units"] = [{"id": str(u["id"]), "title": str(u.get("title") or u["id"]), "deps": u.get("deps") or [],
                           "risk": u["risk"], "requirements": u.get("requirements") or [],
                           "files": u.get("files") or [], "spec": str(u["spec"]).rstrip() + "\n"} for u in units]
        start = f"python {posix(Path(__file__).resolve().parent / 'queue_run.py')} {posix(out)} --repo {posix(self.root)}"
        header = [f"# powerpuff prepare: {self.slug}, {datetime.now():%Y-%m-%d %H:%M}",
                  f"# units: {len(units)} (low {low}, high {len(units) - low})",
                  f"# requirements covered: {n_cov}/{len(ids)}",
                  f"# questions: answered {answered}, defaulted {defaulted}"]
        if self.state.get("lint_accepted"):
            header += [f"# lint accepted: {len(self.state['lint_accepted'])} error(s) (--accept-lint)"]
        header += [f"# start: {start}"]
        if yaml:
            out.write_text("\n".join(header) + "\n" + dump_yaml(queue), encoding="utf-8")
        else:
            out.write_text(json.dumps(queue, indent=1, ensure_ascii=False), encoding="utf-8")
            for h in header:
                print(h)
        p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "queue_run.py"), str(out), "--repo",
                            str(self.root), "--dry-run"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        print(p.stdout.rstrip())
        if p.returncode != 0:
            raise self.fail(f"queue_run rejected {out.name}", [p.stderr.rstrip()] if p.stderr.strip() else [])
        c = self.state["cost"]
        self.log(f"cost (notional USD): {c['total_usd']} by model {c['by_model']}; sessions {c['sessions']}")
        return out

    # ---- run --------------------------------------------------------------------------------------
    def prepare_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        git_dir = Path(self.git("rev-parse", "--absolute-git-dir"))
        exclude = git_dir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        cur = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        if ".powerpuff/" not in cur:
            exclude.write_text(cur + "\n.powerpuff/\n", encoding="utf-8")
        text = self.spec_text()
        if not text.strip():
            raise self.fail("empty spec")
        (self.dir / "spec.md").write_text(text, encoding="utf-8")

    def run(self) -> int:
        try:
            if self.persist:
                self.prepare_dir()
            elif not self.spec_text().strip():
                raise self.fail("empty spec")
            self.stage_lint()
            self.stage_base()
            if self.a.dry_run:
                print(f"dry-run: lint={self.state.get('lint')} base={self.state.get('base')}")
                print(f"dry-run: would run questions + slice ({self.a.model}, <= ${self.a.max_usd:g}), "
                      f"state {self.rel(self.dir)}, output {self.a.out}")
                print("PREPARE: dry-run ok")
                return 0
            self.stage_questions()
            self.stage_slice()
            out = self.stage_emit()
        except Stop as s:
            return s.rc
        print(f"PREPARE: ready {out}", flush=True)
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--model", default="opus")
    ap.add_argument("--advisor", default=None, help="advisor model of the preparation sessions (fable|opus|sonnet); "
                                                    "also written to the emitted queue")
    ap.add_argument("--out", default="queue.yaml")
    ap.add_argument("--max-unit-lines", type=int, default=200)
    ap.add_argument("--max-usd", type=float, default=5, help="cumulative notional budget of the preparation")
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument("--max-outage-min", type=float, default=120, help="total wait for an unreachable API before pausing")
    ap.add_argument("--answers", default="", help="questions.json with filled `answer` fields")
    ap.add_argument("--assume-defaults", action="store_true", help="defaults for unanswered blocking questions")
    ap.add_argument("--accept-lint", action="store_true", help="continue despite spec lint errors")
    ap.add_argument("--skip-base-check", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="stages lint and base only")
    ap.add_argument("--req-regex", default=REQ_REGEX, help="requirement ids that every unit set must cover")
    ap.add_argument("--gate-timeout", type=int, default=1800)
    ap.add_argument("--queue-max-usd", type=float, default=60, help="budget.max_usd of the emitted queue")
    ap.add_argument("--queue-max-hours", type=float, default=8, help="budget.max_hours of the emitted queue")
    a = ap.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=a.repo if Path(a.repo).is_dir() else ".",
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    if top.returncode != 0:
        print(f"PREPARE: failed ({a.repo} is not a git repository)")
        return 1
    return Prepare(a, Path(top.stdout.strip()).resolve()).run()


if __name__ == "__main__":
    sys.exit(main())
