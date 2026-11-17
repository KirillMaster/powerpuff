"""audit_apply - turns the audit answers (answers.yaml) into actions and the next queue. Deterministic, no model calls.

Usage: python audit_apply.py <answers.yaml> --run <run dir> [--repo .] [--queue <original queue.yaml>]
                             [--out <next queue.yaml>] [--dry-run]
  question answers  -> <unit worktree>/.forge/questions.json `answer` (when it still exists) + <run dir>/decisions.md
  merge             -> unit branch merged into the integration branch in a temporary worktree (user's checkout untouched)
  rework/retry/requeue/fix -> units of the next queue (default <run dir>/next-queue.yaml, base = integration branch)
  reject/drop/ignore -> only logged in <run dir>/audit-actions.md
  --dry-run   validate and print the plan, write and change nothing
Exit: 0 ok, 2 some action failed (merge conflict ...), 3 invalid input (all problems listed). Format: docs/audit-format.md
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_cards as ac  # noqa: E402

IDENT = ["-c", "user.name=powerpuff", "-c", "user.email=powerpuff@local"]
NEXT_UNIT = ("rework", "retry", "requeue")
COPIED = ("spec", "budget", "model", "writer", "auto_merge", "assume_defaults", "final")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def posix(p: Path | str) -> str:
    return str(p).replace(chr(92), "/")


def git(cwd: Path | str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.longpaths=true", *args], cwd=cwd,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def mini_yaml(text: str) -> dict:
    """The answers.yaml shape written by cards.html (double-quoted JSON scalars) for a machine without PyYAML."""
    doc: dict = {}
    items: list[dict] = []
    cur: dict | None = None
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("answers:"):
            doc["answers"] = items
            continue
        if s.startswith("- "):
            cur = {}
            items.append(cur)
            s = s[2:]
        key, sep, val = s.partition(":")
        if not sep:
            raise ValueError(f"cannot parse line: {raw!r}")
        val = val.strip()
        (cur if cur is not None and raw[:1] == " " else doc)[key.strip()] = json.loads(val) if val.startswith('"') else val
    return doc


def load_doc(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except ValueError:
        try:
            import yaml
        except ImportError:
            data = mini_yaml(text)
        else:
            data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("must be a mapping")
    return data


def dump_doc(data: dict) -> str:
    try:
        import yaml
    except ImportError:
        return json.dumps(data, indent=1, ensure_ascii=False) + "\n"
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1000)


def next_run_id(run_id: str) -> str:
    m = re.fullmatch(r"(.*)-r(\d+)", run_id)
    return f"{m.group(1)}-r{int(m.group(2)) + 1}" if m else f"{run_id}-r2"


def unit_spec(u: dict, qfile: Path) -> str:
    text = str(u.get("spec") or "")
    if not text.strip() and u.get("spec_file"):
        text = ac.read(qfile.parent / str(u["spec_file"]))
    return text


def check_answers(doc: dict, data: dict, run_id: str) -> tuple[list[dict], list[str]]:
    """Returns (answers in card order, problems)."""
    errs: list[str] = []
    if str(doc.get("run_id")) != run_id:
        errs.append(f"run_id '{doc.get('run_id')}' does not match the run '{run_id}'")
    cards = {c["id"]: c for c in data["cards"]}
    order = {cid: i for i, cid in enumerate(cards)}
    raw = doc.get("answers")
    if not isinstance(raw, list):
        return [], errs + ["answers must be a list"]
    out: list[dict] = []
    seen: set[str] = set()
    for i, a in enumerate(raw):
        if not isinstance(a, dict):
            errs.append(f"answers[{i}] must be a mapping")
            continue
        cid, dec, text = str(a.get("card")), str(a.get("decision")), str(a.get("text") or "")
        c = cards.get(cid)
        if not c:
            errs.append(f"answers[{i}]: unknown card {cid}")
            continue
        if cid in seen:
            errs.append(f"answers[{i}]: duplicate card {cid}")
        seen.add(cid)
        if dec not in ac.ALLOWED[c["kind"]]:
            errs.append(f"{cid}: decision '{dec}' is not allowed for a {c['kind']} card "
                        f"(allowed: {', '.join(ac.ALLOWED[c['kind']]) or 'none'})")
            continue
        if (c["kind"], dec) in ac.REQUIRED_TEXT and not text.strip():
            errs.append(f"{cid}: decision '{dec}' needs a text")
        if dec == "accept-default" and not c["meta"].get("default"):
            errs.append(f"{cid}: the question has no default to accept")
        out.append({"card": cid, "decision": dec, "text": text, "kind": c["kind"], "meta": c["meta"]})
    return sorted(out, key=lambda x: order[x["card"]]), errs


def write_answer(unit: dict, qid: str, answer: str, run_dir: Path) -> str:
    """Writes `answer` into the unit's worktree questions.json and its snapshot copy; returns a note."""
    wrote = []
    wt = unit.get("worktree")
    for label, p in (("worktree", Path(wt) / ".forge" / "questions.json" if wt else None),
                     ("snapshot", run_dir / "forge" / str(unit["id"]) / "questions.json")):
        if not p or not p.is_file():
            continue
        qs = ac.read_json(p)
        if not isinstance(qs, list):
            raise ValueError(f"{p} is not a JSON list of questions")
        hit = [q for q in qs if isinstance(q, dict) and q.get("id") == qid]
        if not hit:
            continue
        hit[0]["answer"] = answer
        p.write_text(json.dumps(qs, indent=1, ensure_ascii=False), encoding="utf-8")
        wrote.append(label)
    return "written to " + "+".join(wrote) if wrote else "logged only (no worktree or snapshot)"


def merge_unit(repo: Path, ib: str, uid: str, title: str, branch: str, dry: bool) -> tuple[bool, str, list[str]]:
    tmp = "<tmp>"
    cmds = [f"git worktree add --detach {tmp} {ib}",
            f"git -C {tmp} merge --no-ff -m \"powerpuff: audit merge {uid} ({title})\" {branch}",
            f"git update-ref refs/heads/{ib} <merge commit> <old {ib}>", f"git worktree remove --force {tmp}"]
    if dry:
        return True, "dry-run", cmds
    for ref in (ib, branch):
        if git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{ref}").returncode != 0:
            return False, f"branch {ref} not found", cmds
    if git(repo, "merge-base", "--is-ancestor", branch, ib).returncode == 0:
        return True, "already merged", cmds
    for line in git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if line == f"branch refs/heads/{ib}":
            return False, f"{ib} is checked out in a worktree; merge by hand", cmds
    old = git(repo, "rev-parse", ib).stdout.strip()
    base_dir = Path(tempfile.mkdtemp(prefix="powerpuff-audit-"))
    wt = base_dir / "wt"
    try:
        r = git(repo, "worktree", "add", "--detach", str(wt), ib)
        if r.returncode != 0:
            return False, f"worktree add failed: {r.stderr.strip()[-200:]}", cmds
        r = git(wt, *IDENT, "merge", "--no-ff", "--no-gpg-sign", "-m", f"powerpuff: audit merge {uid} ({title})", branch)
        if r.returncode != 0:
            git(wt, "merge", "--abort")
            return False, "merge conflict", cmds
        new = git(wt, "rev-parse", "HEAD").stdout.strip()
        r = git(repo, "update-ref", f"refs/heads/{ib}", new, old)
        if r.returncode != 0:
            return False, f"update-ref failed: {r.stderr.strip()[-200:]}", cmds
        return True, f"merged into {ib} ({new[:9]})", cmds
    finally:
        git(repo, "worktree", "remove", "--force", str(wt))
        shutil.rmtree(base_dir, ignore_errors=True)
        git(repo, "worktree", "prune")


def build_next(summary: dict, queue: dict, qfile: Path, picks: dict[str, str], fixes: list[dict],
               human_answers: dict[str, list[str]]) -> dict:
    by_id = {str(u.get("id")): u for u in queue.get("units") or [] if isinstance(u, dict)}
    present = [uid for uid in by_id if uid in picks]

    def resolve(deps: list, seen: set[str]) -> list[str]:
        out: list[str] = []
        for d in map(str, deps or []):
            if d in present:
                out.append(d)
            elif d not in seen:
                seen.add(d)
                out += resolve((by_id.get(d) or {}).get("deps"), seen)
        return list(dict.fromkeys(out))

    units = []
    for uid in present:
        u = by_id[uid]
        spec = unit_spec(u, qfile).rstrip()
        if human_answers.get(uid):
            spec += "\n\nHuman answers:\n" + "\n".join(human_answers[uid])
        if picks[uid].strip():
            spec += "\n\nHuman feedback:\n" + picks[uid].strip()
        units.append({"id": uid, "title": str(u.get("title") or uid), "deps": resolve(u.get("deps"), {uid}),
                      "risk": str(u.get("risk") or "high"), "requirements": list(u.get("requirements") or []),
                      "files": list(u.get("files") or []), "spec": spec + "\n"})
    cmds = ((queue.get("final") or {}).get("commands") or {}) if isinstance(queue.get("final"), dict) else {}
    for fx in fixes:
        spec = (f"Fix the failing final-contour command `{fx['name']}`.\nCommand: {cmds.get(fx['name'], '(unknown)')}\n"
                f"Exit code: {fx['code']}\nOutput (tail):\n{fx['tail']}\n")
        if fx["text"].strip():
            spec += f"\nHuman feedback:\n{fx['text'].strip()}\n"
        units.append({"id": "FIX-" + re.sub(r"[^A-Za-z0-9_.-]", "-", fx["name"]), "title": f"Fix final contour: {fx['name']}",
                      "deps": [], "risk": "high", "requirements": [], "files": [], "spec": spec})
    out: dict = {"run_id": next_run_id(str(summary.get("run_id"))),
                 "base": str(summary.get("integration_branch") or summary.get("base") or "HEAD")}
    for k in COPIED:
        if queue.get(k) is not None:
            v = queue[k]
            if k == "spec" and not Path(str(v)).is_absolute() and (qfile.parent / str(v)).exists():
                v = posix((qfile.parent / str(v)).resolve())
            out[k] = v
    out["units"] = units
    return out


def append_log(path: Path, header: str, body: list[str]) -> None:
    new = not path.is_file()
    with path.open("a", encoding="utf-8") as fh:
        if new:
            fh.write(f"# {path.stem}\n")
        fh.write(f"\n## {header}\n\n" + "\n".join(body) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("answers")
    ap.add_argument("--run", required=True)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--queue")
    ap.add_argument("--out")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    for st in (sys.stdout, sys.stderr):
        st.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    def invalid(errs: list[str]) -> int:
        print("AUDIT: invalid input")
        for e in errs:
            print(f"- {e}")
        return 3

    run_dir = Path(a.run).resolve()
    try:
        data = ac.build_cards(run_dir / "summary.json")
        summary = ac.read_json(run_dir / "summary.json")
        doc = load_doc(Path(a.answers))
    except (OSError, ValueError) as e:
        return invalid([f"cannot read input: {e}"])
    answers, errs = check_answers(doc, data, str(data["run_id"]))
    units = {str(u["id"]): u for u in summary["units"] if isinstance(u, dict) and u.get("id")}  # type: ignore[index]
    ib = str(summary.get("integration_branch") or "")  # type: ignore[union-attr]
    repo = None
    top = git(Path(a.repo) if Path(a.repo).is_dir() else ".", "rev-parse", "--show-toplevel")
    if top.returncode == 0:
        repo = Path(top.stdout.strip())
    if any(x["decision"] == "merge" for x in answers) and (repo is None or not ib):
        errs.append("merge needs a git repository (--repo) and an integration branch in summary.json")
    picks = {x["meta"]["unit"]: x["text"] for x in answers if x["decision"] in NEXT_UNIT}
    fixes = [{**x["meta"], "text": x["text"]} for x in answers if x["decision"] == "fix"]
    queue, qfile = {}, Path(".")
    if picks or fixes:
        qpath = Path(a.queue) if a.queue else Path(str((ac.read_json(run_dir / "state.json") or {}).get("queue_file") or ""))  # type: ignore[union-attr]
        try:
            qfile = qpath.resolve()
            queue = load_doc(qfile)
            known = {str(u.get("id")) for u in queue.get("units") or [] if isinstance(u, dict)}
            errs += [f"unit {u} is not in the original queue {posix(qfile)}" for u in picks if u not in known]
        except (OSError, ValueError) as e:
            errs.append(f"cannot read the original queue (pass --queue): {e}")
    if errs:
        return invalid(errs)

    rows: list[tuple[str, str, str]] = []
    failed = False
    log: list[str] = []
    dec_log: list[str] = []
    human: dict[str, list[str]] = {}
    for x in answers:
        cid, dec, kind, meta = x["card"], x["decision"], x["kind"], x["meta"]
        res = "logged"
        if kind == "question":
            ans = meta["default"] if dec == "accept-default" else x["text"]
            human.setdefault(meta["unit"], []).append(f"- {meta['qid']} {meta['question']}: {ans}")
            dec_log.append(f"- {meta['unit']} {meta['qid']}: {meta['question']} -> [{dec}] {ans}")
            try:
                res = "dry-run" if a.dry_run else write_answer(units[meta["unit"]], meta["qid"], ans, run_dir)
            except (OSError, ValueError) as e:
                res, failed = f"FAILED: {e}", True
        elif dec == "merge":
            u = units[meta["unit"]]
            ok, res, cmds = merge_unit(repo, ib, meta["unit"], str(u.get("title", "")), str(u.get("branch")), a.dry_run)  # type: ignore[arg-type]
            failed |= not ok
            res = res if ok else f"FAILED: {res}"
            log += [f"### {cid}: {res}", "", "```", *cmds, "```", ""]
        elif dec in NEXT_UNIT or dec == "fix":
            res = "next queue"
        rows.append((cid, dec, res))
        if dec not in ("merge",) and kind != "question":
            log.append(f"- {cid}: {dec} ({res}){': ' + x['text'] if x['text'].strip() else ''}")

    next_out = None
    if picks or fixes:
        nq = build_next(summary, queue, qfile, picks, fixes, human)  # type: ignore[arg-type]
        next_out = Path(a.out) if a.out else run_dir / "next-queue.yaml"
        if not a.dry_run:
            next_out.write_text(dump_doc(nq), encoding="utf-8")
    if not a.dry_run:
        stamp = f"apply {now_iso()} (answers decided_at {doc.get('decided_at')})"
        if dec_log:
            append_log(run_dir / "decisions.md", stamp, dec_log)
        if log:
            append_log(run_dir / "audit-actions.md", stamp, log)
    w = max([len(r[0]) for r in rows] + [4])
    print(f"{'card'.ljust(w)}  {'decision'.ljust(14)}  result")
    for r in rows:
        print(f"{r[0].ljust(w)}  {r[1].ljust(14)}  {r[2]}")
    if next_out:
        print(f"next queue: {posix(next_out)}{' (dry-run, not written)' if a.dry_run else ''}")
    print("AUDIT: " + ("some actions failed" if failed else "ok"))
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
