"""audit_cards - one self-contained HTML file of decision cards for the audit of an autonomous run. No model calls.

Usage: python audit_cards.py <run dir | summary.json> [--repo .] [--out <run dir>/cards.html] [--max-diff-lines 400]
Reads summary.json written by queue_run.py, the per-unit .forge snapshots (<run dir>/forge/<unit>/) and, when the repo and
the unit branch are available, `git diff <integration branch>...<unit branch>`. Missing pieces are skipped, never fatal.
The page works from file:// (inline CSS/JS, no external requests); the decisions are exported as answers.yaml.
Exit: 0 ok, 3 summary.json missing or unreadable. Format: docs/audit-format.md
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

OPTIONS = {
    "question": [("accept-default", "Принять по умолчанию", "none"), ("answer", "Свой ответ", "required")],
    "high-risk": [("merge", "Слить", "none"), ("reject", "Отклонить", "optional"), ("rework", "Переделать", "required")],
    "failure": [("retry", "Повторить как есть", "none"), ("rework", "Переделать", "required"), ("drop", "Отбросить", "none")],
    "blocked": [("requeue", "В очередь", "none"), ("drop", "Отбросить", "none")],
    "final": [("fix", "Создать единицу-исправление", "optional"), ("ignore", "Игнорировать", "none")],
    "summary": [],
}
ALLOWED = {k: [o[0] for o in v] for k, v in OPTIONS.items()}
REQUIRED_TEXT = {(k, o[0]) for k, v in OPTIONS.items() for o in v if o[2] == "required"}
WAITING = ("blocked", "skipped-budget", "needs-answer")
GROUPS = {"question": "Вопросы", "high-risk": "Единицы высокого риска", "failure": "Провалы",
          "blocked": "Заблокированные и не запущенные", "final": "Финальный контур", "summary": "Итог"}


def read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def read_json(p: Path) -> object:
    try:
        return json.loads(read(p))
    except ValueError:
        return None


def head(text: str, n: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[:n]) + (f"\n... ({len(lines) - n} more lines)" if len(lines) > n else "")


def tail(text: str, n: int) -> str:
    lines = text.strip().splitlines()
    return ("... \n" if len(lines) > n else "") + "\n".join(lines[-n:])


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=repo, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def forge_dir(run_dir: Path, u: dict) -> Path | None:
    wt = u.get("worktree")
    for p in (Path(wt) / ".forge" if wt else None, run_dir / "forge" / str(u.get("id"))):
        if p and p.is_dir():
            return p
    return None


def diff_info(repo: Path | None, base: str, branch: str, max_lines: int) -> dict:
    """{stat, patch, note}: every part may be empty when the repo or the branch is not available."""
    out = {"stat": "", "patch": "", "note": ""}
    if repo is None or git(repo, "rev-parse", "--show-toplevel").returncode != 0:
        out["note"] = "репозиторий недоступен: дифф не получен"
        return out
    for ref in (base, branch):
        if not ref or git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}").returncode != 0:
            out["note"] = f"ссылка '{ref}' не найдена: дифф не получен"
            return out
    out["stat"] = git(repo, "diff", "--stat", f"{base}...{branch}").stdout.strip()
    out["patch"] = head(git(repo, "diff", f"{base}...{branch}").stdout, max_lines)
    return out


def questions_of(fd: Path | None, u: dict) -> list[dict]:
    data = read_json(fd / "questions.json") if fd else None
    if isinstance(data, list):
        return [q for q in data if isinstance(q, dict) and q.get("id")]
    out = []
    for s in u.get("questions") or []:
        m = re.match(r"\W*Q(\d+)", str(s))
        if m:
            out.append({"id": f"Q{m.group(1)}", "class": "non-blocking", "question": str(s), "default": "",
                        "wrong_guess_cost": "", "answer": None})
    return out


def answered(q: dict) -> bool:
    return q.get("answer") is not None and bool(str(q["answer"]).strip())


def gate_lines(check: object) -> tuple[str, str]:
    """(gate results, failing output tail) from check.json."""
    if not isinstance(check, dict):
        return "нет данных", ""
    lines, bad = [], []
    for part in ("coverage", "frozen"):
        if isinstance(check.get(part), dict):
            lines.append(f"{part}: {'ok' if check[part].get('ok') else 'FAIL'}")
    results = ((check.get("gates") or {}).get("results") or {}) if isinstance(check.get("gates"), dict) else {}
    for name, r in results.items():
        ok = isinstance(r, dict) and r.get("ok")
        lines.append(f"gate {name}: {'ok' if ok else 'FAIL'}")
        if not ok and isinstance(r, dict) and r.get("tail"):
            bad.append(f"[{name}]\n{tail(str(r['tail']), 20)}")
    for part in ("coverage", "frozen", "guard"):
        p = check.get(part)
        if isinstance(p, dict) and not p.get("ok", True):
            bad.append(f"[{part}] {json.dumps(p, ensure_ascii=False)[:600]}")
    return "\n".join(lines) or "нет данных", "\n\n".join(bad)


def f(label: str, value: object, pre: bool = False) -> dict:
    return {"label": label, "value": str(value), "pre": pre}


def card(cid: str, kind: str, title: str, fields: list, details: list | None = None, meta: dict | None = None) -> dict:
    return {"id": cid, "kind": kind, "title": title, "fields": fields, "details": details or [], "meta": meta or {},
            "options": [{"key": k, "label": lbl, "text": t} for k, lbl, t in OPTIONS[kind]]}


def build_cards(summary_path: Path, repo: Path | None = None, max_diff: int = 400) -> dict:
    run_dir = summary_path.parent
    s = read_json(summary_path)
    if not isinstance(s, dict):
        raise ValueError(f"{summary_path} is not a readable summary.json")
    units = [u for u in s.get("units") or [] if isinstance(u, dict) and u.get("id")]
    ib = str(s.get("integration_branch") or "")
    qcards: list[tuple[int, dict]] = []
    info_q: list[dict] = []
    for u in units:
        uid = str(u["id"])
        for q in questions_of(forge_dir(run_dir, u), u):
            if answered(q):
                continue
            qid, cls = str(q["id"]), str(q.get("class") or "non-blocking")
            text = re.sub(rf"^\W*{re.escape(qid)}\W*", "", str(q.get("question", "")))
            if cls == "info":
                info_q.append({"unit": uid, "id": qid, "question": text})
                continue
            qcards.append((0 if cls == "blocking" else 1, card(
                f"question:{uid}:{qid}", "question", f"{uid} {qid}: {text[:90]}",
                [f("Единица", f"{uid} - {u.get('title', '')}"), f("Класс", cls), f("Вопрос", text, True),
                 f("По умолчанию", q.get("default") or "-", True), f("Цена ошибки", q.get("wrong_guess_cost") or "-")],
                meta={"unit": uid, "qid": qid, "default": str(q.get("default") or ""), "question": text})))
    cards = [c for _, c in sorted(qcards, key=lambda x: x[0])]
    for u in (x for x in units if x.get("status") == "done"):
        uid, fd = str(u["id"]), forge_dir(run_dir, u)
        req = read(fd / "requirements.md") if fd else ""
        req_lines = [x.strip() for x in req.splitlines() if re.match(r"\s*[-*]?\s*\**[RD]\d+", x)]
        gates, _ = gate_lines(read_json(fd / "check.json") if fd else None)
        check = read_json(fd / "check.json") if fd else None
        guard = json.dumps(check.get("guard"), ensure_ascii=False)[:600] if isinstance(check, dict) and \
            check.get("guard") is not None else "нет данных"
        d = diff_info(repo, ib or str(s.get("base") or ""), str(u.get("branch") or ""), max_diff)
        details = [{"label": "Патч", "text": d["patch"] or d["note"] or "пусто"}]
        if fd and (fd / "REPORT.md").is_file():
            details.append({"label": "Отчёт", "text": head(read(fd / "REPORT.md"), 60)})
        cards.append(card(f"unit:{uid}", "high-risk", f"{uid}: {u.get('title', '')}",
                          [f("Риск", u.get("risk", "")), f("Ветка", u.get("branch", "")),
                           f("Решения и требования", "\n".join(req_lines) or "-", True),
                           f("Дифф-стат", d["stat"] or d["note"] or "-", True), f("Гейты", gates, True),
                           f("Guard", guard, True)], details, {"unit": uid}))
    for u in (x for x in units if x.get("status") == "failed"):
        uid, fd = str(u["id"]), forge_dir(run_dir, u)
        _, bad = gate_lines(read_json(fd / "check.json") if fd else None)
        details = []
        if fd and (fd / "REPORT.md").is_file():
            details.append({"label": "Отчёт (начало)", "text": head(read(fd / "REPORT.md"), 40)})
        if bad:
            details.append({"label": "Упавшие проверки", "text": bad})
        log = read(run_dir / f"{uid}.log")
        if log:
            details.append({"label": "Лог (конец)", "text": tail(log, 30)})
        cards.append(card(f"unit:{uid}", "failure", f"{uid}: {u.get('title', '')}",
                          [f("Причина", u.get("reason") or "-", True), f("Ветка", u.get("branch", ""))], details,
                          {"unit": uid}))
    for u in (x for x in units if x.get("status") in WAITING):
        uid = str(u["id"])
        cards.append(card(f"unit:{uid}", "blocked", f"{uid}: {u.get('status')}",
                          [f("Единица", f"{uid} - {u.get('title', '')}"), f("Статус", u.get("status")),
                           f("Причина", u.get("reason") or "-", True)], meta={"unit": uid}))
    for name, r in (s.get("final") or {}).items():
        if isinstance(r, dict) and not r.get("ok"):
            cards.append(card(f"final:{name}", "final", f"Финальная команда {name} упала",
                              [f("Код выхода", r.get("code")), f("Вывод (конец)", tail(str(r.get("tail") or ""), 30), True)],
                              meta={"name": str(name), "code": r.get("code"), "tail": str(r.get("tail") or "")[-2000:]}))
    counts: dict[str, int] = {}
    for u in units:
        counts[str(u.get("status"))] = counts.get(str(u.get("status")), 0) + 1
    cost = s.get("cost") or {}
    by_model = "\n".join(f"{m}: {v}" for m, v in (cost.get("by_model") or {}).items()) or "-"
    summary = card("summary", "summary", f"Итог прогона {s.get('run_id', '')}: {s.get('status', '')}",
                   [f("Единицы", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "-"),
                    f("Стоимость, USD", cost.get("total_usd", 0)), f("По моделям", by_model, True),
                    f("Время, мин", s.get("elapsed_min", 0)), f("База", s.get("base", "")),
                    f("Интеграционная ветка", ib)])
    summary["info_questions"] = info_q
    cards.append(summary)
    return {"run_id": str(s.get("run_id") or run_dir.name), "groups": GROUPS, "cards": cards}


def render_html(data: dict) -> str:
    blob = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e") \
        .replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return PAGE.replace("__TITLE__", "powerpuff audit").replace("__DATA__", blob)


PAGE = r"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#fff;--fg:#1b1f24;--mut:#667;--card:#f6f8fa;--bd:#d0d7de;--acc:#0969da;--ok:#1a7f37}
@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--mut:#8b949e;--card:#161b22;--bd:#30363d;--acc:#58a6ff;--ok:#3fb950}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0 auto;max-width:900px;padding:0 12px 80px}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--bd);padding:8px 0;display:flex;gap:12px;align-items:center;z-index:2}
header b{flex:1}h3{margin:24px 0 6px;color:var(--mut)}
.card{background:var(--card);border:1px solid var(--bd);border-radius:6px;padding:10px 12px;margin:8px 0}
.card.cur{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc)}.card.done{border-left:4px solid var(--ok)}
.card h2{font-size:15px;margin:0 0 6px}.k{color:var(--mut);font-size:12px}
pre{white-space:pre-wrap;word-break:break-word;margin:2px 0 6px;font:12px/1.4 ui-monospace,monospace}
.opts{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
button{background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:4px;padding:3px 10px;cursor:pointer}
button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
textarea,input{width:100%;box-sizing:border-box;background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:4px;margin-top:6px}
#out{height:160px;font:12px ui-monospace,monospace}
</style></head><body>
<header><b>powerpuff audit <span id="rid"></span></b><span id="prog"></span><button id="exp">Export (e)</button></header>
<p class="k">j/k - следующая/предыдущая карточка, 1-9 - выбор варианта, e - экспорт. Решения хранятся в localStorage.</p>
<div id="root"></div>
<div id="exportbox" hidden><h3>answers.yaml</h3><textarea id="out" readonly></textarea></div>
<script id="data" type="application/json">__DATA__</script>
<script>
(function(){
var D=JSON.parse(document.getElementById('data').textContent),cards=D.cards,KEY='powerpuff-audit:'+D.run_id;
var dec={},cur=0,els={};
try{dec=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){}
function save(){try{localStorage.setItem(KEY,JSON.stringify(dec))}catch(e){}}
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e}
function q(s){return JSON.stringify(String(s))}
function decided(c){var d=dec[c.id];if(!d)return false;var o=c.options.filter(function(o){return o.key===d.decision})[0];
 if(!o)return false;return o.text!=='required'||!!(d.text||'').trim()}
function paint(){var n=0,m=0;cards.forEach(function(c,i){var e=els[c.id];if(!c.options.length)return;m++;
 var ok=decided(c);if(ok)n++;e.classList.toggle('done',ok);e.classList.toggle('cur',i===cur);
 e.querySelectorAll('button').forEach(function(b){b.classList.toggle('on',(dec[c.id]||{}).decision===b.dataset.key)})});
 document.getElementById('prog').textContent='decided '+n+' / '+m}
function choose(c,key){dec[c.id]={decision:key,text:(dec[c.id]||{}).text||''};save();paint()}
function focus(i){cur=Math.max(0,Math.min(cards.length-1,i));paint();var e=els[cards[cur].id];if(e.scrollIntoView)e.scrollIntoView({block:'center'})}
function build(){var root=document.getElementById('root'),last='';document.getElementById('rid').textContent=D.run_id;
 cards.forEach(function(c,i){
  if(c.kind!==last){root.appendChild(el('h3','',D.groups[c.kind]||c.kind));last=c.kind}
  var e=el('section','card');els[c.id]=e;e.addEventListener('click',function(){cur=i;paint()});
  e.appendChild(el('div','k',c.kind+' | '+c.id));e.appendChild(el('h2','',c.title));
  c.fields.forEach(function(x){var r=el('div');r.appendChild(el('span','k',x.label+': '));
   if(x.pre){r.appendChild(el('pre','',x.value))}else{r.appendChild(el('span','',x.value))}e.appendChild(r)});
  c.details.forEach(function(x){var d=el('details');d.appendChild(el('summary','',x.label));d.appendChild(el('pre','',x.text));e.appendChild(d)});
  if(c.info_questions&&c.info_questions.length){var d=el('details');d.appendChild(el('summary','','Информационные вопросы ('+c.info_questions.length+')'));
   d.appendChild(el('pre','',c.info_questions.map(function(x){return x.unit+' '+x.id+': '+x.question}).join('\n')));e.appendChild(d)}
  if(c.options.length){var o=el('div','opts');c.options.forEach(function(op,j){var b=el('button','',(j+1)+' '+op.label);b.dataset.key=op.key;
    b.addEventListener('click',function(){choose(c,op.key)});o.appendChild(b)});e.appendChild(o);
   var t=el('textarea');t.rows=2;t.placeholder='Комментарий / ответ';t.value=(dec[c.id]||{}).text||'';
   t.addEventListener('input',function(){dec[c.id]=dec[c.id]||{decision:'',text:''};dec[c.id].text=t.value;save();paint()});e.appendChild(t)}
  root.appendChild(e)});paint()}
function exportYaml(){var out=['run_id: '+q(D.run_id),'decided_at: '+q(new Date().toISOString())],n=0,rows=[];
 cards.forEach(function(c){if(decided(c)){var d=dec[c.id];n++;rows.push('  - card: '+q(c.id),'    decision: '+d.decision,'    text: '+q(d.text||''))}});
 out.push(n?'answers:':'answers: []');var y=out.concat(rows).join('\n')+'\n';
 document.getElementById('exportbox').hidden=false;document.getElementById('out').value=y;
 try{var a=el('a');a.href=URL.createObjectURL(new Blob([y],{type:'text/yaml'}));a.download='answers.yaml';document.body.appendChild(a);a.click();a.remove()}catch(e){}}
document.getElementById('exp').addEventListener('click',exportYaml);
document.addEventListener('keydown',function(ev){var t=ev.target.tagName;
 if(t==='TEXTAREA'||t==='INPUT'){if(ev.key==='Escape')ev.target.blur();return}
 if(ev.ctrlKey||ev.metaKey||ev.altKey)return;
 if(ev.key==='j')focus(cur+1);else if(ev.key==='k')focus(cur-1);else if(ev.key==='e')exportYaml();
 else if(/^[1-9]$/.test(ev.key)){var c=cards[cur],o=c.options[+ev.key-1];if(o)choose(c,o.key)}});
build();focus(0);
})();
</script></body></html>
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--out")
    ap.add_argument("--max-diff-lines", type=int, default=400)
    a = ap.parse_args(argv)
    for st in (sys.stdout, sys.stderr):
        st.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    p = Path(a.run)
    summary = p if p.is_file() else p / "summary.json"
    try:
        data = build_cards(summary, Path(a.repo) if Path(a.repo).is_dir() else None, a.max_diff_lines)
    except ValueError as e:
        print(f"AUDIT: {e}")
        return 3
    out = Path(a.out) if a.out else summary.parent / "cards.html"
    out.write_text(render_html(data), encoding="utf-8")
    decisions = sum(1 for c in data["cards"] if c["options"])
    print(f"AUDIT: {len(data['cards'])} cards ({decisions} to decide) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
