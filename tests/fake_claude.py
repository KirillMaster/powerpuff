"""Scripted stand-in for `claude -p --output-format stream-json` used by tests/test_forge_run.py.

Env: FAKE_CLAUDE_SCENARIO (json path, required), FAKE_CLAUDE_LOG (call log, default <scenario>.calls.jsonl),
FAKE_CLAUDE_PIDFILE (file with the driver pid, only for the `kill_driver` action).
Scenario: {"stages": {"<stage>": [entry, ...]}}; call N of a stage uses entry N (the last one repeats). Missing stage
or entry "default" = the stage's happy-path behaviour. Entry keys:
  base (bool)  start from the stage default writes/appends, `write`/`append` overlay them
  write {path: text}, append {path: text}, delete [path]
  result       ok (default) | rate_limit | rate_event (rate_limit_event rejected + synthetic message, like the real CLI)
               | rate_warning (ok, with an allowed_warning rate_limit_event) | is_error | no_result | api_error
               epoch (rate_limit / rate_event), text, cost, exit_code
  kill_driver  terminate the driver process, then exit without a result event
Stages are recognised by the first line of the prompt: audit tests topup vacuous implement fix accept gapfix retry
and, for the plain engine, ptests (turn 1) pbuild (turn 2) panswers. Every session reports session_id `sess-<n>`; the
call log keeps `resume` (the id given to --resume).
"""

import json
import os
import signal
import sys
import time
from pathlib import Path

FIRST_LINES = [("You are the spec auditor", "audit"), ("You write the acceptance tests", "tests"),
               ("You complete the acceptance tests", "topup"), ("You implement a spec", "implement"),
               ("The acceptance tests pass without any implementation", "vacuous"),
               ("The deterministic check failed", "fix"), ("You are the independent acceptance reviewer", "accept"),
               ("The independent acceptance review found gaps", "gapfix"), ("Not finished", "retry"),
               ("Turn 1 of 2", "ptests"), ("Turn 2 of 2", "pbuild"), ("Human decisions arrived", "panswers"),
               ("These requirement ids", "pcover")]

REQS = "R1: foo returns 42\nR2: bar returns 7\nD1: rounding -> floor - existing code\nQ1: anything else?\n"
GATE = 'python -c "import app; assert app.foo() == 42 and app.bar() == 7"'
GOOD_APP = "def foo():\n    return 42\n\n\ndef bar():\n    return 7\n"
TEST_FILE = ("from app import foo, bar\n\n\ndef test_r1():\n    # R1\n    assert foo() == 42\n\n\n"
             "def test_r2():\n    # R2\n    assert bar() == 7\n")
EXTRA_TEST = "def test_r3():\n    # R3\n    pass\n"
VACUOUS_TEST = "def test_r1_new():\n    # R1\n    assert False\n"
PLAIN_Q = "Q1 [non-blocking]: anything else? | default: no | cost of a wrong guess: low\n"
NO_GAPS = json.dumps({"gaps": [], "questions": []})

DEFAULT_WRITE = {
    "audit": {".forge/requirements.md": REQS, ".forge/digest.md": "digest\n",
              ".forge/gates.json": json.dumps({"format_fix": None, "gates": {"unit": GATE}})},
    "tests": {"tests/test_acceptance.py": TEST_FILE, ".forge/acceptance-files.txt": "tests/test_acceptance.py\n"},
    "topup": {"tests/test_acceptance_extra.py": EXTRA_TEST},
    "vacuous": {"tests/test_acceptance_vacuous.py": VACUOUS_TEST},
    "implement": {"app.py": GOOD_APP}, "fix": {"app.py": GOOD_APP}, "gapfix": {"app.py": GOOD_APP},
    "accept": {".forge/gaps.json": NO_GAPS},
    "ptests": {"tests/test_acceptance.py": TEST_FILE, ".forge/acceptance-files.txt": "tests/test_acceptance.py\n",
               ".forge/requirements.md": PLAIN_Q},
    "pbuild": {"app.py": GOOD_APP},
}
DEFAULT_APPEND = {"topup": {".forge/acceptance-files.txt": "tests/test_acceptance_extra.py\n"},
                  "vacuous": {".forge/acceptance-files.txt": "tests/test_acceptance_vacuous.py\n"}}


def put(rel: str, text: str, mode: str) -> None:
    p = Path(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, mode, encoding="utf-8", newline="\n") as f:
        f.write(text)


def main() -> int:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    argv = sys.argv[1:]
    model = argv[argv.index("--model") + 1] if "--model" in argv else None
    resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
    settings = argv[argv.index("--settings") + 1] if "--settings" in argv else None
    advisor = json.loads(Path(settings).read_text(encoding="utf-8")).get("advisorModel") if settings else None
    prompt = sys.stdin.read()
    first = prompt.lstrip().splitlines()[0] if prompt.strip() else ""
    stage = next((name for head, name in FIRST_LINES if first.startswith(head)), "unknown")

    scn_path = Path(os.environ["FAKE_CLAUDE_SCENARIO"])
    scn = json.loads(scn_path.read_text(encoding="utf-8"))
    counters_path = Path(str(scn_path) + ".counters.json")
    counters = json.loads(counters_path.read_text(encoding="utf-8")) if counters_path.is_file() else {}
    idx = counters.get(stage, 0)
    counters[stage] = idx + 1
    counters["_total"] = counters.get("_total", 0) + 1
    n = counters["_total"]
    counters_path.write_text(json.dumps(counters), encoding="utf-8")
    sid = f"sess-{n}"

    entries = (scn.get("stages") or {}).get(stage) or ["default"]
    entry = entries[min(idx, len(entries) - 1)]
    if entry == "default":
        entry = {"base": True}
    log = Path(os.environ.get("FAKE_CLAUDE_LOG") or str(scn_path) + ".calls.jsonl")
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"n": n, "stage": stage, "index": idx, "model": model, "has_resume": resume is not None,
                            "resume": resume, "session_id": sid, "prompt": prompt, "advisor": advisor,
                            "cache_5m": os.environ.get("FORCE_PROMPT_CACHING_5M"),
                            "cache_1h": os.environ.get("ENABLE_PROMPT_CACHING_1H")}) + "\n")

    if entry.get("kill_driver"):
        pidfile = Path(os.environ["FAKE_CLAUDE_PIDFILE"])
        for _ in range(200):
            if pidfile.is_file() and pidfile.read_text().strip():
                break
            time.sleep(0.1)
        os.kill(int(pidfile.read_text().strip()), signal.SIGTERM)
        time.sleep(0.5)
        return 1

    writes = {**(DEFAULT_WRITE.get(stage, {}) if entry.get("base") else {}), **entry.get("write", {})}
    appends = {**(DEFAULT_APPEND.get(stage, {}) if entry.get("base") else {}), **entry.get("append", {})}
    for rel in entry.get("delete", []):
        Path(rel).unlink(missing_ok=True)
    for rel, text in writes.items():
        put(rel, text, "w")
    for rel, text in appends.items():
        put(rel, text, "a")

    kind = entry.get("result", "ok")
    cost = entry.get("cost", 0.25)
    cum = counters.setdefault("_cum", {})
    cost = round(cum.get(resume, 0) + cost, 4) if resume else cost  # like the real CLI: --resume reports the session total
    cum[sid] = cost
    counters_path.write_text(json.dumps(counters), encoding="utf-8")
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": f"{stage} working"}]}}),
          flush=True)
    if advisor and entry.get("advisor", "ok") != "silent":
        result = {"type": "advisor_redacted_result", "encrypted_content": "x"} if entry.get("advisor", "ok") == "ok" \
            else {"type": "advisor_tool_result_error", "error_code": "unavailable"}
        print(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "server_tool_use", "id": "srv1", "name": "advisor", "input": {}}]}}), flush=True)
        print(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "advisor_tool_result", "tool_use_id": "srv1", "content": result}]}}), flush=True)
    if kind == "no_result":
        return int(entry.get("exit_code", 0))
    if kind == "api_error":
        err = "API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)"
        print(json.dumps({"type": "assistant", "error": "server_error", "is_api_error_message": True,
                          "message": {"content": [{"type": "text", "text": err}]}}), flush=True)
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "api_error": "connection_failed",
                          "terminal_reason": "api_error", "result": err, "total_cost_usd": 0, "num_turns": 1,
                          "session_id": sid}), flush=True)
        return int(entry.get("exit_code", 1))
    if kind in ("rate_event", "rate_warning"):
        epoch = entry.get("epoch", int(time.time()) + 3600)
        status = "rejected" if kind == "rate_event" else "allowed_warning"
        print(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": status, "resetsAt": epoch, "rateLimitType": "five_hour", "overageStatus": "rejected",
            "unifiedWindows": {"five_hour": {"utilization": 1 if status == "rejected" else 0.8, "resetsAt": epoch,
                                             "status": status}}}}), flush=True)
    if kind == "rate_event":
        msg = "You've hit your 5-hour limit · resets 5:10am (Asia/Bangkok)"
        print(json.dumps({"type": "assistant", "error": "rate_limit", "is_api_error_message": True,
                          "message": {"model": "<synthetic>", "content": [{"type": "text", "text": msg}]}}), flush=True)
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": msg,
                          "total_cost_usd": 0, "num_turns": 1, "session_id": sid}), flush=True)
        return int(entry.get("exit_code", 1))
    if kind == "rate_warning":
        kind = "ok"
    is_error = kind in ("rate_limit", "is_error")
    text = entry.get("text", "done")
    if kind == "rate_limit":
        text = f"Claude AI usage limit reached|{entry.get('epoch', int(time.time()) + 3600)}"
    print(json.dumps({"type": "result", "subtype": "error" if is_error else "success", "is_error": is_error,
                      "result": text, "total_cost_usd": cost, "num_turns": 3, "session_id": sid,
                      "modelUsage": {model: {"costUSD": cost}}}), flush=True)
    return int(entry.get("exit_code", 0))


if __name__ == "__main__":
    sys.exit(main())
