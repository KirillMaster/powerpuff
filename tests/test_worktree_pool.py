"""queue_run: parallel default and the warm worktree pool (finished unit worktrees keep bin/ obj/ for the next unit)."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import queue_run  # noqa: E402

IDENT = ["-c", "user.name=t", "-c", "user.email=t@t"]


def g(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *IDENT, *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def qdict(**top: object) -> dict:
    return {"run_id": "r1", "units": [{"id": u, "spec": f"Build {u}"} for u in ("U1", "U2", "U3")], **top}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POWERPUFF_POOL", raising=False)
    monkeypatch.delenv("POWERPUFF_PARALLEL", raising=False)


def make_queue(tmp_path: Path, **top: object) -> queue_run.Queue:
    repo = tmp_path / "my repo"
    repo.mkdir()
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    (repo / ".gitignore").write_text("bin/\n", encoding="utf-8")
    g(repo, "init", "-q", "-b", "main")
    g(repo, "add", "-A")
    g(repo, "commit", "-q", "--no-gpg-sign", "-m", "init")
    qfile = tmp_path / "queue.json"
    qfile.write_text("{}", encoding="utf-8")
    cfg, errs = queue_run.normalize(qdict(**top), qfile, repo)
    assert not errs, errs
    g(repo, "branch", cfg["integration_branch"], "main")
    q = queue_run.Queue(cfg, argparse.Namespace(only="", max_outage_min=1), repo, qfile)
    q.run_dir.mkdir(parents=True)
    return q


def pool_items(q: queue_run.Queue) -> list[str]:
    p = q.pool_dir() / "pool.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else []


def test_parallel_default_is_three_env_and_queue_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    qf = tmp_path / "q.json"
    qf.write_text("{}", encoding="utf-8")
    norm = lambda **top: queue_run.normalize(qdict(**top), qf, tmp_path)[0]["parallel"]  # noqa: E731
    assert norm() == 3
    monkeypatch.setenv("POWERPUFF_PARALLEL", "5")
    assert norm() == 5
    assert norm(parallel=2) == 2
    monkeypatch.setenv("POWERPUFF_PARALLEL", "junk")
    assert norm() == 3


def test_pooled_worktree_keeps_build_outputs_but_nothing_else(tmp_path: Path) -> None:
    q = make_queue(tmp_path)
    wt1 = q.prepare_worktree("U1")
    (wt1 / "bin").mkdir()
    (wt1 / "bin" / "x.dll").write_text("built", encoding="utf-8")
    (wt1 / ".forge").mkdir()
    (wt1 / ".forge" / "state.json").write_text("{}", encoding="utf-8")
    (wt1 / "junk.txt").write_text("x", encoding="utf-8")
    (wt1 / "README.md").write_text("changed\n", encoding="utf-8")
    q.release_worktree(wt1)
    assert wt1.exists() and pool_items(q) == [str(wt1)]
    wt2 = q.prepare_worktree("U2")
    assert wt2 == wt1 and q.live_wt("U2") == wt1 and (wt2 / ".git").exists() and pool_items(q) == []
    assert q.prepare_worktree("U2") == wt1
    assert (wt2 / "bin" / "x.dll").read_text(encoding="utf-8") == "built"
    assert not (wt2 / ".forge").exists() and not (wt2 / "junk.txt").exists()
    assert (wt2 / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert g(wt2, "status", "--porcelain") == ""
    assert g(wt2, "rev-parse", "--abbrev-ref", "HEAD") == q.branch("U2")
    assert g(wt2, "rev-parse", "HEAD") == g(q.repo, "rev-parse", q.cfg["integration_branch"])


def test_pool_follows_the_new_integration_tip(tmp_path: Path) -> None:
    q = make_queue(tmp_path)
    wt1 = q.prepare_worktree("U1")
    q.release_worktree(wt1)
    integ = q.integration()
    (integ / "new.txt").write_text("n", encoding="utf-8")
    g(integ, "add", "-A")
    g(integ, "commit", "-q", "--no-gpg-sign", "-m", "more")
    wt2 = q.prepare_worktree("U2")
    assert (wt2 / "new.txt").is_file()


def test_released_branch_can_be_deleted_and_recreated(tmp_path: Path) -> None:
    q = make_queue(tmp_path)
    wt1 = q.prepare_worktree("U1")
    br = q.branch("U1")
    q.release_worktree(wt1)
    g(q.repo, "branch", "-D", br)
    wt = q.prepare_worktree("U1")
    assert wt == wt1 and g(wt, "rev-parse", "--abbrev-ref", "HEAD") == br


def test_fallback_to_a_fresh_worktree_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    q = make_queue(tmp_path)
    q.release_worktree(q.prepare_worktree("U1"))
    real = queue_run.git

    def flaky(cwd: Path | str, *args: str) -> subprocess.CompletedProcess:
        if args[:1] == ("clean",):
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return real(cwd, *args)

    monkeypatch.setattr(queue_run, "git", flaky)
    wt2 = q.prepare_worktree("U2")
    assert (wt2 / ".git").exists() and pool_items(q) == []
    assert g(wt2, "rev-parse", "--abbrev-ref", "HEAD") == q.branch("U2")


def test_stale_pool_entry_is_ignored(tmp_path: Path) -> None:
    q = make_queue(tmp_path)
    q.pool_dir().mkdir(parents=True)
    (q.pool_dir() / "pool.json").write_text(json.dumps([str(tmp_path / "gone")]), encoding="utf-8")
    assert (q.prepare_worktree("U1") / ".git").exists()


@pytest.mark.parametrize("how", ["env", "queue"])
def test_pool_opt_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    if how == "env":
        monkeypatch.setenv("POWERPUFF_POOL", "0")
        q = make_queue(tmp_path)
    else:
        q = make_queue(tmp_path, worktree_pool="off")
    wt = q.prepare_worktree("U1")
    q.release_worktree(wt)
    assert not wt.exists() and pool_items(q) == []


def test_pooled_tree_is_never_moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    q = make_queue(tmp_path)
    real = queue_run.git
    calls: list[tuple[str, ...]] = []

    def spy(cwd: Path | str, *args: str) -> subprocess.CompletedProcess:
        calls.append(args)
        return real(cwd, *args)

    monkeypatch.setattr(queue_run, "git", spy)
    q.release_worktree(q.prepare_worktree("U1"))
    q.prepare_worktree("U2")
    assert not [c for c in calls if c[:2] == ("worktree", "move")]


def test_pool_is_capped_and_oldest_removed_for_real(tmp_path: Path) -> None:
    q = make_queue(tmp_path)
    q.cfg["units"] += [{"id": f"X{i}"} for i in range(8)]
    wts = [q.prepare_worktree(f"X{i}") for i in range(8)]
    for w in wts:
        q.release_worktree(w)
    items = pool_items(q)
    assert len(items) == queue_run.POOL_MAX and all((Path(p) / ".git").exists() for p in items)
    assert items == [str(w) for w in wts[-queue_run.POOL_MAX:]] and not any(w.exists() for w in wts[:-queue_run.POOL_MAX])
