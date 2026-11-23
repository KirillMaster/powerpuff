"""Tests for scripts/audit_cards.py and scripts/audit_apply.py: hand-written run dirs (tests/fixtures/audit), small
throwaway git repos in tmp_path, no model and no network. REPORT.md snapshots are written into the tmp copy of the run."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FIXTURE = HERE / "fixtures" / "audit" / "run_full"
sys.path.insert(0, str(ROOT / "scripts"))
import audit_apply  # noqa: E402
import audit_cards  # noqa: E402

REPORTS = {"U2": "# FORGE REPORT - done\n\nEndpoint /items implemented.\n",
           "U3": "# FORGE REPORT - gates red\n\nThe exporter fails the lint gate.\n"}
HOSTILE = "</script><img src=x onerror=alert(1)>"
QUEUE = {
    "run_id": "fx", "base": "main", "model": "opus", "budget": {"max_usd": 30},
    "final": {"commands": {"lint": "ruff check ."}},
    "units": [
        {"id": "U1", "title": "Parser", "risk": "low", "files": ["a.py"], "spec": "Build the parser."},
        {"id": "U2", "title": "Endpoint", "risk": "high", "deps": ["U1"], "files": ["a.py", "b.py"], "spec": "Endpoint."},
        {"id": "U3", "title": "Exporter", "risk": "high", "spec": "Exporter."},
        {"id": "U4", "title": "Report page", "deps": ["U3"], "spec": "Page."},
        {"id": "U5", "title": "Cache", "spec": "Cache."},
        {"id": "U6", "title": "Auth", "risk": "high", "spec": "Auth."},
        {"id": "U7", "title": "Docs", "risk": "high", "spec": "Docs."},
    ],
}


def copy_run(tmp_path: Path, name: str = "run") -> Path:
    run = tmp_path / name
    shutil.copytree(FIXTURE, run)
    for uid, text in REPORTS.items():
        (run / "forge" / uid / "REPORT.md").write_text(text, encoding="utf-8")
    return run


def git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                       cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True)
    return p.stdout.strip()


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "c0")
    for b in ("powerpuff/run-fx", "powerpuff/fx/U2", "powerpuff/fx/U7"):
        git(repo, "branch", b)
    git(repo, "checkout", "-q", "powerpuff/fx/U2")
    (repo / "b.txt").write_text("u2\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "u2")
    git(repo, "checkout", "-q", "powerpuff/fx/U7")
    (repo / "a.txt").write_text("u7\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "u7")
    git(repo, "checkout", "-q", "powerpuff/run-fx")
    (repo / "a.txt").write_text("integration\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "ib")
    git(repo, "checkout", "-q", "main")
    return repo


def cards_of(run: Path, repo: Path | None = None) -> dict:
    return audit_cards.build_cards(run / "summary.json", repo)


def html_json(html: str) -> dict:
    m = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S)
    assert m
    return json.loads(m.group(1))


def write_answers(tmp_path: Path, answers: list[dict], run_id: str = "fx") -> Path:
    p = tmp_path / "answers.json"
    p.write_text(json.dumps({"run_id": run_id, "decided_at": "2026-11-17T10:00:00Z", "answers": answers}), encoding="utf-8")
    return p


def apply(args: list) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "audit_apply.py"), *map(str, args)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


# ---- cards -----------------------------------------------------------------------------------------------------

def test_cards_one_of_each_status_and_order(tmp_path):
    ids = [c["id"] for c in cards_of(copy_run(tmp_path))["cards"]]
    assert ids == ["question:U6:Q1", "question:U1:Q1", "unit:U2", "unit:U7", "unit:U3", "unit:U4", "unit:U5", "unit:U6",
                   "final:lint", "summary"]
    kinds = {c["id"]: c["kind"] for c in cards_of(copy_run(tmp_path, "r2"))["cards"]}
    assert (kinds["unit:U2"], kinds["unit:U3"], kinds["unit:U4"], kinds["unit:U5"], kinds["unit:U6"]) == \
        ("high-risk", "failure", "blocked", "blocked", "blocked")


def test_info_questions_collapsed_and_answered_skipped(tmp_path):
    data = cards_of(copy_run(tmp_path))
    ids = [c["id"] for c in data["cards"]]
    assert "question:U1:Q2" not in ids and "question:U1:Q3" not in ids
    assert data["cards"][-1]["info_questions"][0]["id"] == "Q2"


def test_summary_card_totals(tmp_path):
    s = cards_of(copy_run(tmp_path))["cards"][-1]
    text = json.dumps(s["fields"])
    assert "opus: 9.0" in text and "12.5" in text and "powerpuff/run-fx" in text and "done 2" in text
    assert s["options"] == []


def test_high_risk_card_has_diff_gates_and_requirements(tmp_path):
    run, repo = copy_run(tmp_path), make_repo(tmp_path)
    c = {x["id"]: x for x in cards_of(run, repo)["cards"]}["unit:U2"]
    fields = {x["label"]: x["value"] for x in c["fields"]}
    assert "b.txt" in fields["Diff stat"] and "gate lint: ok" in fields["Gates"]
    assert "D1" in fields["Decisions and requirements"] and "R2" in fields["Decisions and requirements"]
    assert "+u2" in c["details"][0]["text"]


def test_patch_is_truncated(tmp_path):
    run, repo = copy_run(tmp_path), make_repo(tmp_path)
    data = audit_cards.build_cards(run / "summary.json", repo, max_diff=3)
    patch = {x["id"]: x for x in data["cards"]}["unit:U2"]["details"][0]["text"]
    assert "more lines" in patch


def test_failure_card_evidence(tmp_path):
    c = {x["id"]: x for x in cards_of(copy_run(tmp_path))["cards"]}["unit:U3"]
    text = json.dumps(c["details"])
    assert "FORGE REPORT - gates red" in text and "F401 unused import" in text


def test_html_self_contained_with_all_cards_and_hostile_text_neutralised(tmp_path):
    run = copy_run(tmp_path)
    out = tmp_path / "cards.html"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "audit_cards.py"), str(run), "--repo", str(tmp_path / "none"),
                        "--out", str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    html = out.read_text(encoding="utf-8")
    assert not re.search(r"(src|href)\s*=\s*[\"']?https?://", html) and "http://" not in html and "https://" not in html
    for c in cards_of(run)["cards"]:
        assert c["id"] in html
    assert "<img" not in html and HOSTILE not in html and html.count("</script>") == 2
    assert "prefers-color-scheme" in html and "localStorage" in html and "decided " in html


def test_embedded_json_parses_back(tmp_path):
    run = copy_run(tmp_path)
    data = cards_of(run)
    back = html_json(audit_cards.render_html(data))
    assert back == data
    assert any(HOSTILE in json.dumps(c) for c in back["cards"])


def test_missing_forge_snapshots_and_repo_degrade(tmp_path):
    run = copy_run(tmp_path)
    shutil.rmtree(run / "forge")
    data = cards_of(run, None)
    ids = [c["id"] for c in data["cards"]]
    assert "unit:U2" in ids and "unit:U3" in ids and ids[-1] == "summary"
    u2 = {x["id"]: x for x in data["cards"]}["unit:U2"]
    assert "unavailable" in json.dumps(u2["fields"], ensure_ascii=False)
    audit_cards.render_html(data)


def test_unknown_branch_degrades(tmp_path):
    run, repo = copy_run(tmp_path), make_repo(tmp_path)
    git(repo, "branch", "-D", "powerpuff/fx/U7")
    c = {x["id"]: x for x in cards_of(run, repo)["cards"]}["unit:U7"]
    assert "not found" in json.dumps(c["fields"], ensure_ascii=False)


def test_unreadable_summary_exit_3(tmp_path):
    (tmp_path / "summary.json").write_text("nope", encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "audit_cards.py"), str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 3


# ---- apply: validation -----------------------------------------------------------------------------------------

def test_validation_lists_all_problems_exit_3(tmp_path):
    run = copy_run(tmp_path)
    ans = write_answers(tmp_path, [
        {"card": "unit:U99", "decision": "merge", "text": ""},
        {"card": "unit:U2", "decision": "retry", "text": ""},
        {"card": "question:U6:Q1", "decision": "answer", "text": ""},
        {"card": "summary", "decision": "merge", "text": ""},
    ], run_id="other")
    r = apply([ans, "--run", run, "--repo", tmp_path, "--dry-run"])
    assert r.returncode == 3
    for needle in ("does not match the run", "unknown card unit:U99", "'retry' is not allowed for a high-risk",
                   "needs a text", "summary"):
        assert needle in r.stdout
    assert not (run / "decisions.md").exists()


def test_missing_queue_is_invalid_when_next_units_requested(tmp_path):
    run = copy_run(tmp_path)
    ans = write_answers(tmp_path, [{"card": "unit:U5", "decision": "requeue", "text": ""}])
    r = apply([ans, "--run", run, "--repo", tmp_path])
    assert r.returncode == 3 and "original queue" in r.stdout


def test_mini_yaml_reads_what_cards_html_exports():
    text = ('run_id: "fx"\ndecided_at: "2026-11-17T10:00:00.000Z"\nanswers:\n  - card: "question:U6:Q1"\n'
            '    decision: answer\n    text: "line1\\nline \\"2\\""\n  - card: "unit:U2"\n    decision: merge\n    text: ""\n')
    doc = audit_apply.mini_yaml(text)
    assert doc["run_id"] == "fx" and doc["answers"][0]["text"] == 'line1\nline "2"'
    assert doc["answers"][1] == {"card": "unit:U2", "decision": "merge", "text": ""}


# ---- apply: questions, logging ---------------------------------------------------------------------------------

def test_question_answer_written_to_worktree_and_logged(tmp_path):
    run = copy_run(tmp_path)
    wt = tmp_path / "wt" / "U6"
    (wt / ".forge").mkdir(parents=True)
    shutil.copy(run / "forge" / "U6" / "questions.json", wt / ".forge" / "questions.json")
    s = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    next(u for u in s["units"] if u["id"] == "U6")["worktree"] = str(wt)
    (run / "summary.json").write_text(json.dumps(s), encoding="utf-8")
    ans = write_answers(tmp_path, [{"card": "question:U6:Q1", "decision": "answer", "text": "oauth2"},
                                   {"card": "question:U1:Q1", "decision": "accept-default", "text": ""},
                                   {"card": "unit:U3", "decision": "drop", "text": ""}])
    r = apply([ans, "--run", run, "--repo", tmp_path])
    assert r.returncode == 0, r.stdout + r.stderr
    qs = json.loads((wt / ".forge" / "questions.json").read_text(encoding="utf-8"))
    assert qs[0]["answer"] == "oauth2"
    snap = json.loads((run / "forge" / "U1" / "questions.json").read_text(encoding="utf-8"))
    assert snap[0]["answer"] == "RFC 7231"
    log = (run / "decisions.md").read_text(encoding="utf-8")
    assert "U6 Q1" in log and "oauth2" in log and "[accept-default] RFC 7231" in log
    assert "unit:U3: drop" in (run / "audit-actions.md").read_text(encoding="utf-8")
    again = apply([ans, "--run", run, "--repo", tmp_path])
    assert again.returncode == 3 and "unknown card question:U6:Q1" in again.stdout
    assert (run / "decisions.md").read_text(encoding="utf-8").count("## apply") == 1


def test_dry_run_changes_nothing(tmp_path):
    run = copy_run(tmp_path)
    ans = write_answers(tmp_path, [{"card": "question:U1:Q1", "decision": "accept-default", "text": ""},
                                   {"card": "unit:U5", "decision": "requeue", "text": ""}])
    qf = tmp_path / "queue.json"
    qf.write_text(json.dumps(QUEUE), encoding="utf-8")
    r = apply([ans, "--run", run, "--repo", tmp_path, "--queue", qf, "--dry-run"])
    assert r.returncode == 0 and "dry-run" in r.stdout
    assert not (run / "decisions.md").exists() and not (run / "next-queue.yaml").exists()
    assert json.loads((run / "forge" / "U1" / "questions.json").read_text(encoding="utf-8"))[0]["answer"] is None


# ---- apply: merge ----------------------------------------------------------------------------------------------

def test_merge_clean_and_conflict(tmp_path):
    run, repo = copy_run(tmp_path), make_repo(tmp_path)
    head_before = git(repo, "rev-parse", "HEAD")
    old_ib = git(repo, "rev-parse", "powerpuff/run-fx")
    ans = write_answers(tmp_path, [{"card": "unit:U2", "decision": "merge", "text": ""},
                                   {"card": "unit:U7", "decision": "merge", "text": ""}])
    r = apply([ans, "--run", run, "--repo", repo])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "merge conflict" in r.stdout
    assert git(repo, "show", "powerpuff/run-fx:b.txt") == "u2"
    assert git(repo, "show", "powerpuff/run-fx:a.txt") == "integration"
    assert git(repo, "rev-parse", "powerpuff/run-fx") != old_ib
    assert git(repo, "rev-parse", "HEAD") == head_before and git(repo, "branch", "--show-current") == "main"
    assert git(repo, "status", "--porcelain") == ""
    assert len(git(repo, "worktree", "list").splitlines()) == 1
    actions = (run / "audit-actions.md").read_text(encoding="utf-8")
    assert "git update-ref refs/heads/powerpuff/run-fx" in actions and "FAILED: merge conflict" in actions


def test_merge_dry_run_and_already_merged(tmp_path):
    run, repo = copy_run(tmp_path), make_repo(tmp_path)
    old_ib = git(repo, "rev-parse", "powerpuff/run-fx")
    ans = write_answers(tmp_path, [{"card": "unit:U2", "decision": "merge", "text": ""}])
    assert apply([ans, "--run", run, "--repo", repo, "--dry-run"]).returncode == 0
    assert git(repo, "rev-parse", "powerpuff/run-fx") == old_ib and not (run / "audit-actions.md").exists()
    assert apply([ans, "--run", run, "--repo", repo]).returncode == 0
    r = apply([ans, "--run", run, "--repo", repo])
    assert r.returncode == 0 and "already merged" in r.stdout


# ---- apply: next queue -----------------------------------------------------------------------------------------

def next_queue(tmp_path: Path, answers: list[dict], queue: dict = QUEUE) -> tuple[dict, Path, Path]:
    run, repo = copy_run(tmp_path), make_repo(tmp_path)
    qf = tmp_path / "queue.json"
    qf.write_text(json.dumps(queue), encoding="utf-8")
    r = apply([write_answers(tmp_path, answers), "--run", run, "--repo", repo, "--queue", qf])
    assert r.returncode == 0, r.stdout + r.stderr
    return audit_apply.load_doc(run / "next-queue.yaml"), qf, repo


def test_next_queue_rework_retry_requeue_fix(tmp_path):
    nq, qf, repo = next_queue(tmp_path, [
        {"card": "question:U6:Q1", "decision": "answer", "text": "oauth2"},
        {"card": "unit:U2", "decision": "rework", "text": "use cursor pagination"},
        {"card": "unit:U3", "decision": "retry", "text": ""},
        {"card": "unit:U4", "decision": "requeue", "text": ""},
        {"card": "unit:U5", "decision": "drop", "text": ""},
        {"card": "unit:U6", "decision": "requeue", "text": "keep it simple"},
        {"card": "final:lint", "decision": "fix", "text": "only a.py"}])
    assert nq["run_id"] == "fx-r2" and nq["base"] == "powerpuff/run-fx"
    assert nq["model"] == "opus" and nq["budget"] == {"max_usd": 30}
    by_id = {u["id"]: u for u in nq["units"]}
    assert list(by_id) == ["U2", "U3", "U4", "U6", "FIX-lint"]
    assert by_id["U2"]["deps"] == [] and by_id["U4"]["deps"] == ["U3"]
    assert by_id["U2"]["spec"].rstrip().endswith("Human feedback:\nuse cursor pagination")
    assert "Human feedback" not in by_id["U3"]["spec"]
    assert "Human answers:\n- Q1 Which auth scheme?: oauth2" in by_id["U6"]["spec"]
    assert "Command: ruff check ." in by_id["FIX-lint"]["spec"] and "E501" in by_id["FIX-lint"]["spec"]
    assert by_id["FIX-lint"]["spec"].rstrip().endswith("only a.py")
    assert audit_apply.load_doc(qf)["run_id"] == "fx"


def test_next_queue_deps_resolved_through_absent_units(tmp_path):
    q = json.loads(json.dumps(QUEUE))
    q["units"][4]["deps"] = ["U1"]
    q["units"][3]["deps"] = ["U5"]
    nq, _, _ = next_queue(tmp_path, [{"card": "unit:U4", "decision": "requeue", "text": ""}], q)
    assert nq["units"][0]["deps"] == []


def test_next_queue_accepted_by_queue_run(tmp_path):
    nq, _, repo = next_queue(tmp_path, [
        {"card": "unit:U2", "decision": "rework", "text": "use cursor pagination"},
        {"card": "unit:U3", "decision": "retry", "text": ""},
        {"card": "unit:U4", "decision": "requeue", "text": ""},
        {"card": "final:lint", "decision": "fix", "text": ""}])
    nqf = tmp_path / "run" / "next-queue.yaml"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "queue_run.py"), str(nqf), "--repo", str(repo), "--dry-run"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FIX-lint" in r.stdout
