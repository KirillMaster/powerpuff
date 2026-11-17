"""Deterministic pre-run lint of a spec directory: reject specs the unattended agents would have to guess about.

Input: a yamlkit-style spec dir with `spec.yaml` / `spec.md` and optionally `tasks.yaml` / `tasks.md`
(YAML wins over Markdown for the same artifact; plan, contracts, data-model, checklists are not read).
YAML keys relied on: spec - status, user_stories[id, title, description, acceptance_scenarios[id], acceptance_criteria[id],
test_scenarios[id]], functional_requirements[id, text, story_refs], success_criteria / non_functional_requirements[id,
description, story_refs]; tasks - tasks[id, title, description, story_ref, requirement_refs, depends_on, steps,
technical_notes[path, note], test_scenarios]. Markdown is parsed by regex on the same id conventions
(AS-n, TS-n, FR-n, AC-n, SC-n/NFR-n, USn or "User Story n", Tn).

Usage: python spec_lint.py <spec-dir> [--repo .] [--max-task-lines 600] [--json]
Exit: 0 no errors (warnings allowed), 1 errors, 3 input error (dir missing, no spec file, PyYAML missing for a YAML spec).

Rules (severity):
  as-without-test (E)           acceptance scenario/criterion with no linked test scenario
  story-without-nfr (E)         state-changing story with no non-functional criterion (see NFR_CATEGORIES)
  missing-path (E)              existing file a task modifies is absent under --repo; path-unverified (W) when undecidable
  tasks-too-long (E)            tasks file longer than --max-task-lines
  deferred-without-owner (E)    deferred / TODO / out-of-scope item without owner, story, task or issue reference
  unresolved-clarification (E)  any [NEEDS CLARIFICATION marker
  task-without-requirement (W)  task links to no story / FR id
  requirement-without-task (E)  FR or user story no task references
  dangling-reference (E)        referenced id (TS->AS, AS->TS, task->US/FR/AS, depends_on->task, FR->US) is not defined
  ambiguous-wording (W)         unquantified vague word in a requirement (AMBIGUOUS_WORDS)
  status-draft (W)              spec status is Draft or empty
  yaml-parse (E)                YAML syntax error with line;  unknown-structure (W): unrecognised layout, lint continues

Limits of the heuristics:
  * The yamlkit format has no explicit AS->TS link: a scenario counts as linked when a TS/AS text names the other id,
    an AS carries a test key, or (implicit) its story owns at least one TS and none of the story's TS names an AS.
  * Acceptance criteria are only checked for stories without scenarios; task-level TS/AC ids are task-local.
  * State change, NFR category and ambiguity detection are keyword lists (stems, EN/RU) - they miss synonyms and
    match words out of context. NFR text must sit in the story's criteria or in an NFR/SC entry that names the story.
  * Path ownership (create vs modify) is decided by the nearest verb in the same clause; a path another task creates,
    or whose directory another task creates, is never reported missing.
  * Markdown scenarios without ids (numbered Given/When/Then lists) are invisible; prefer the YAML form.
"""

import argparse
import json
import posixpath
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

STATE_CHANGE_VERBS = {
    "en": [r"creat(?:e|es|ed|ing|ion)", r"updat(?:e|es|ed|ing)", r"delet(?:e|es|ed|ing|ion)", r"sav(?:e|es|ed|ing)",
           r"writ(?:e|es|ing)", r"wrote", r"written", r"persist\w*", r"send\w*", r"sent", r"transfer\w*",
           r"pay", r"pays", r"paid", r"paying", r"payments?", r"migrat\w+"],
    "ru": [r"созда\w*", r"измен\w*", r"удал\w*", r"сохран\w*", r"записа\w*", r"записыва\w*", r"отправ\w*",
           r"перевест\w*", r"перевод\w*", r"оплат\w*", r"мигр\w+"],
}

NFR_CATEGORIES = {
    "performance": {
        "en": ["performance", "latency", "budget", "throughput", "p95", "response time", "time limit", "timeout"],
        "ru": ["производительн", "задержк", "бюджет", "время отклика", "лимит времени", "таймаут"],
    },
    "failure": {
        "en": ["failure", "restart", "retry", "retries", "idempoten", "recover", "crash", "resume", "interrupt"],
        "ru": ["сбой", "сбои", "перезапуск", "повторн", "идемпотент", "восстанов", "прерыван"],
    },
    "concurrency": {
        "en": ["concurren", "duplicate", "parallel", "race condition", "simultaneous", "single-flight"],
        "ru": ["конкурент", "дубл", "параллел", "гонк", "одновременн"],
    },
    "integrity": {
        "en": ["integrity", "consisten", "atomic", "transaction", "checksum", "no data loss"],
        "ru": ["целостност", "согласованн", "атомарн", "транзакц", "потери данных"],
    },
    "security": {
        "en": ["security", "authoriz", "authentic", "permission", "secret", "encrypt", "forbidden", "access control"],
        "ru": ["безопасн", "авториз", "аутентиф", "права доступа", "шифров", "секрет"],
    },
}

AMBIGUOUS_WORDS = {
    "en": ["fast", "quickly", "user-friendly", "robust", "scalable", "appropriate", "as needed", "etc.",
           "should be able to", "easy", "efficient", "reasonable", "flexible", "intuitive", "seamless"],
    "ru": ["быстро", "удобно", "надёжно", "надежно", "при необходимости", "и т.д.", "и т.п.", "как правило",
           "интуитивно", "гибко", "достаточно"],
}

CREATE_VERBS = [r"creat\w*", r"add\w*", r"new", r"introduc\w*", r"scaffold\w*", r"generat\w*",
                r"созда\w*", r"добав\w*", r"нов(?:ый|ая|ое|ые|ых|ого|ому)"]
MODIFY_VERBS = [r"modif\w*", r"updat\w*", r"edit\w*", r"chang\w*", r"replac\w*", r"fix\w*", r"refactor\w*",
                r"renam\w*", r"extend\w*", r"patch\w*", r"remov\w*", r"delet\w*", r"switch\w*",
                r"измен\w*", r"обнов\w*", r"замен\w*", r"правк\w*", r"исправ\w*", r"переимен\w*", r"удал\w*",
                r"расшир\w*", r"перевест\w*", r"перевод\w*", r"переписа\w*"]
MARKER_NEW = re.compile(r"^\s*[\(\[]\s*(?:new|creat\w*|нов\w*|добав\w*|созда\w*)\b", re.I)
MARKER_EXISTING = re.compile(r"^\s*[\(\[]\s*(?:existing|exists|modif\w*|updat\w*|существ\w*|измен\w*)\b", re.I)

DEFERRED_KEYS = ("deferred", "out_of_scope", "follow_ups", "followups", "deferred_items", "todo", "later")
DEFERRED_MARKERS = [r"(?-i:TODO|TBD)", r"deferred", r"(?:do|handle|add|implement|fix|decide|address|revisit)\s+(?:this\s+|it\s+)?later",
                    r"отложен\w*", r"позже"]
DEFERRED_HEADING = re.compile(r"(?i)deferred|out of scope|follow-?ups?|later|отложен|вне рамок|за рамками")
OWNER_KEYS = ("owner", "owners", "assignee", "assigned_to", "responsible", "story", "story_ref", "story_refs",
              "task", "task_ref", "task_refs", "issue", "issue_ref", "ticket", "tracker", "ref", "refs", "owner_ref")
OWNER_RE = re.compile(
    r"(?i:\bowner\s*[:=]\s*\S+|владелец\s*[:=]\s*\S+|ответственн\w*\s*[:=]\s*\S+)"
    r"|(?<![\w@])@\w{2,}|\bUS\d+\b|\bT\d+\b|#\d+"
    r"|\b(?!(?:AS|TS|FR|AC|EC|SC|NFR|UTF|ISO|SHA)-)[A-Z][A-Z0-9]{1,9}-\d+\b")
CLARIFICATION = "[NEEDS CLARIFICATION"
TEST_KEYS = ("test", "tests", "test_id", "test_ids", "test_ref", "test_refs", "ts", "ts_refs", "verified_by", "covered_by")
NFR_KEYS = ("success_criteria", "non_functional_requirements", "nonfunctional_requirements", "nfr", "quality_attributes")
TASK_TEXT_KEYS = ("title", "description", "context", "steps", "technical_notes", "files", "paths", "file")
BARE_EXTENSIONS = {"py", "js", "ts", "tsx", "jsx", "cs", "csproj", "sln", "json", "yaml", "yml", "md", "toml", "txt", "cfg",
                   "ini", "sh", "ps1", "html", "css", "scss", "sql", "go", "rs", "java", "kt", "rb", "php", "xml", "lock"}
NOT_PATHS = {"node.js", "next.js", "vue.js", "express.js", "nuxt.js", "three.js", "d3.js", "asp.net"}

AS_RE = re.compile(r"\bAS-\d+\b")
AC_RE = re.compile(r"\b(?:AS|AC)-\d+\b")
TS_RE = re.compile(r"\bTS-\d+\b")
FR_RE = re.compile(r"\bFR-\d+\b")
US_RE = re.compile(r"\bUS\d+\b")
T_RE = re.compile(r"\bT\d+\b")
PATH_RE = re.compile(r"(?<![\w/\\<>:.\-@*])((?:\.{0,2}/)?[\w.\-@]+(?:/[\w.\-@]+)+\.[A-Za-z]\w{0,7})(?![\w/*<])")
TICK_RE = re.compile(r"`([\w.\-@/]+)`")
SEGMENT_BREAK = re.compile(r"[;\n]|(?<=[a-z0-9)\]»\"'])\.\s")

BLOCK_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:#{1,6}\s+)?(?:\[[ xX]\]\s*)?[*_`\[(]*"
                      r"((?:AS|TS|FR|AC|EC|SC|NFR)-\d+|US\d+|T\d+)\b")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
STORY_HEAD_RE = re.compile(r"(?i)^user story\s+(\d+)")
STATUS_MD_RE = re.compile(r"(?i)\*\*status\*\*\s*:\s*([^|\n*]*)|^status\s*:\s*(.*)$", re.M)


def _alt(parts: list[str]) -> str:
    return "(?:" + "|".join(parts) + ")"


STATE_RE = re.compile(r"(?<!\w)" + _alt([p for v in STATE_CHANGE_VERBS.values() for p in v]) + r"(?!\w)", re.I)
CREATE_RE = re.compile(r"(?<!\w)" + _alt(CREATE_VERBS) + r"(?!\w)", re.I)
MODIFY_RE = re.compile(r"(?<!\w)" + _alt(MODIFY_VERBS) + r"(?!\w)", re.I)
DEFER_RE = re.compile(r"(?<!\w)" + _alt(DEFERRED_MARKERS) + r"(?!\w)", re.I)
AMBIGUOUS_RE = re.compile(r"(?<!\w)(" + "|".join(re.escape(w) for v in AMBIGUOUS_WORDS.values() for w in v) + r")(?!\w)", re.I)

class InputError(Exception):
    pass


class LDict(dict):
    line = 0
    vlines: dict = {}


class LList(list):
    line = 0
    item_lines: list = []


def _make_loader():
    class Loader(yaml.SafeLoader):
        pass

    def seq(loader, node):
        data = LList()
        data.line = node.start_mark.line + 1
        data.item_lines = [n.start_mark.line + 1 for n in node.value]
        yield data
        data.extend(loader.construct_sequence(node))

    def mapping(loader, node):
        data = LDict()
        data.line = node.start_mark.line + 1
        data.vlines = {k.value: v.start_mark.line + 1 for k, v in node.value if isinstance(k, yaml.ScalarNode)}
        yield data
        data.update(loader.construct_mapping(node))

    Loader.add_constructor("tag:yaml.org,2002:seq", seq)
    Loader.add_constructor("tag:yaml.org,2002:map", mapping)
    return Loader


def _walk(obj, line=0, item=None):
    """Yields (string, line, nearest enclosing id) for every scalar string under obj."""
    if isinstance(obj, dict):
        if obj.get("id") not in (None, ""):
            item = str(obj["id"])
        for k, v in obj.items():
            yield from _walk(v, getattr(obj, "vlines", {}).get(k, getattr(obj, "line", line)), item)
    elif isinstance(obj, list):
        lines = getattr(obj, "item_lines", [])
        for i, v in enumerate(obj):
            yield from _walk(v, lines[i] if i < len(lines) else line, item)
    elif isinstance(obj, str):
        yield obj, line, item


def _flat(obj) -> str:
    return "\n".join(s for s, _, _ in _walk(obj))


def _vline(d, key, default=0):
    return getattr(d, "vlines", {}).get(key, getattr(d, "line", default))


class Ctx:
    def __init__(self):
        self.findings = []
        self.stories, self.as_, self.ac, self.ts, self.fr, self.nfr = {}, {}, {}, {}, {}, {}
        self.tasks, self.task_ts, self.deferred = [], [], []
        self.status, self.status_file, self.status_line = None, None, None
        self.tasks_file, self.task_lines = None, 0
        self._seen = set()

    def add(self, rule, severity, file, line, message, item=None):
        self.findings.append({"rule": rule, "severity": severity, "file": file, "line": line or None,
                              "message": message, "item": item})

    def unknown(self, file, message, line=None):
        self.add("unknown-structure", "WARN", file, line, message)

    def once(self, key) -> bool:
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


def _rec(kind, rid, file, line, text, main=None, story=None):
    return {"kind": kind, "id": rid, "file": file, "line": line, "text": text, "main": main if main is not None else text,
            "story": story}


def _items(ctx, d, keys, fname, allow_str=False):
    for key in keys:
        if key not in d:
            continue
        v = d[key]
        if v is None:
            return
        if not isinstance(v, list):
            ctx.unknown(fname, f"'{key}' is not a list", _vline(d, key))
            return
        lines = getattr(v, "item_lines", [])
        for i, x in enumerate(v):
            if isinstance(x, dict) or (allow_str and isinstance(x, str)):
                yield i, x, (x.line if isinstance(x, LDict) else (lines[i] if i < len(lines) else _vline(d, key)))
            else:
                ctx.unknown(fname, f"'{key}[{i}]' is not a mapping", lines[i] if i < len(lines) else None)
        return


def _id(d) -> str:
    return str(d.get("id", "") or "").strip() if isinstance(d, dict) else ""


def _register_deferred(ctx, fname, line, text, item, owner):
    if ctx.once(("deferred", fname, line, text)):
        ctx.deferred.append({"file": fname, "line": line, "text": text, "item": item, "owner": owner})


def _scan_markers(ctx, doc, fname):
    for text, line, item in _walk(doc):
        if CLARIFICATION in text:
            if ctx.once(("clar", fname, line, text)):
                ctx.add("unresolved-clarification", "ERROR", fname, line, "unresolved [NEEDS CLARIFICATION marker", item)
        elif DEFER_RE.search(text):
            _register_deferred(ctx, fname, line, text, item, bool(OWNER_RE.search(text)))


def _scan_deferred_keys(ctx, obj, fname, item=None, line=0):
    if isinstance(obj, dict):
        if obj.get("id") not in (None, ""):
            item = str(obj["id"])
        for k, v in obj.items():
            vl = _vline(obj, k, line)
            if str(k).lower() in DEFERRED_KEYS:
                elems = v if isinstance(v, list) else [v]
                lines = getattr(v, "item_lines", [])
                for i, e in enumerate(elems):
                    if e in (None, ""):
                        continue
                    text = _flat(e) if isinstance(e, (dict, list)) else str(e)
                    if CLARIFICATION in text:
                        continue
                    owner = bool(OWNER_RE.search(text))
                    if isinstance(e, dict):
                        owner = owner or any(e.get(o) for o in OWNER_KEYS)
                    _register_deferred(ctx, fname, lines[i] if i < len(lines) else vl, text, item, owner)
            else:
                _scan_deferred_keys(ctx, v, fname, item, vl)
    elif isinstance(obj, list):
        lines = getattr(obj, "item_lines", [])
        for i, v in enumerate(obj):
            _scan_deferred_keys(ctx, v, fname, item, lines[i] if i < len(lines) else line)


def _read_spec_yaml(ctx, doc, fname):
    if not isinstance(doc, dict):
        ctx.unknown(fname, "spec root is not a mapping")
        return
    st = doc.get("status")
    ctx.status, ctx.status_file, ctx.status_line = (str(st).strip() if st is not None else ""), fname, _vline(doc, "status", 1)
    for _, s, line in _items(ctx, doc, ("user_stories", "stories"), fname):
        sid = _id(s)
        if not sid:
            ctx.unknown(fname, "user story without id", line)
            continue
        own = f"{s.get('title', '')}\n{s.get('description', '')}"
        story = _rec("us", sid, fname, line, own, story=sid)
        story.update({"as": [], "ac": [], "ts": [], "accept": ""})
        ctx.stories[sid] = story
        parts = []
        for _, a, al in _items(ctx, s, ("acceptance_scenarios",), fname):
            aid = _id(a)
            if not aid:
                ctx.unknown(fname, f"acceptance scenario without id in {sid}", al)
                continue
            r = _rec("as", aid, fname, al, _flat(a), story=sid)
            r["test_key"] = any(a.get(k) for k in TEST_KEYS)
            ctx.as_[aid] = r
            story["as"].append(aid)
            parts.append(r["text"])
        for _, c, cl in _items(ctx, s, ("acceptance_criteria",), fname):
            cid = _id(c)
            if cid:
                r = _rec("ac", cid, fname, cl, _flat(c), main=str(c.get("description", "") or c.get("text", "")), story=sid)
                r["test_key"] = any(c.get(k) for k in TEST_KEYS)
                ctx.ac[cid] = r
                story["ac"].append(cid)
                parts.append(r["text"])
        for _, t, tl in _items(ctx, s, ("test_scenarios",), fname):
            tid = _id(t)
            if tid:
                r = _rec("ts", tid, fname, tl, _flat(t), story=sid)
                ctx.ts[tid] = r
                story["ts"].append(tid)
                parts.append(r["text"])
        story["accept"] = "\n".join(parts)
    for _, f, line in _items(ctx, doc, ("functional_requirements", "requirements"), fname):
        fid = _id(f)
        if fid:
            ctx.fr[fid] = _rec("fr", fid, fname, line, _flat(f), main=str(f.get("text", "") or f.get("description", "")))
        else:
            ctx.unknown(fname, "requirement without id", line)
    for key in NFR_KEYS:
        for i, n, line in _items(ctx, doc, (key,), fname, allow_str=True):
            nid = _id(n) if isinstance(n, dict) else ""
            text = _flat(n) if isinstance(n, dict) else n
            main = str(n.get("description", "") or n.get("text", "")) if isinstance(n, dict) else n
            nid = nid or f"{key}[{i}]"
            ctx.nfr[nid] = _rec("nfr", nid, fname, line, text, main=main)
    _scan_markers(ctx, doc, fname)
    _scan_deferred_keys(ctx, doc, fname)


def _read_tasks_yaml(ctx, doc, fname):
    if not isinstance(doc, dict):
        ctx.unknown(fname, "tasks root is not a mapping")
        return
    for i, t, line in _items(ctx, doc, ("tasks",), fname):
        tid = _id(t)
        if not tid:
            ctx.unknown(fname, f"task #{i + 1} without id", line)
            tid = f"task[{i + 1}]"
        texts = []
        for key in TASK_TEXT_KEYS:
            if key not in t:
                continue
            v = t[key]
            if key == "technical_notes" and isinstance(v, list):
                for n in v:
                    if isinstance(n, dict):
                        texts.append((f"{n.get('path', '')}: {n.get('note', '')}", getattr(n, "line", line)))
                    else:
                        texts.extend((s, ln) for s, ln, _ in _walk(n, line))
            else:
                texts.extend((s, ln) for s, ln, _ in _walk(v, _vline(t, key, line)))
        flat = _flat({k: v for k, v in t.items() if k not in ("test_scenarios",)})
        ctx.tasks.append({"id": tid, "file": fname, "line": line, "texts": texts, "text": flat,
                          "depends": set(T_RE.findall(_flat(t.get("depends_on")))) - {tid}})
        for _, ts, tl in _items(ctx, t, ("test_scenarios",), fname):
            ctx.task_ts.append(_rec("ts", _id(ts), fname, tl, _flat(ts), story=tid))
    _scan_markers(ctx, doc, fname)
    _scan_deferred_keys(ctx, doc, fname)


def _read_md(ctx, text, fname, is_spec):
    lines = text.splitlines()
    if is_spec:
        m = STATUS_MD_RE.search(text)
        ctx.status = ((m.group(1) or m.group(2) or "") if m else "").strip()
        ctx.status_file = fname
        ctx.status_line = text[:m.start()].count("\n") + 1 if m else 1
    story = task = None
    story_level = task_level = 0
    block = None
    in_deferred = False
    blocks = []

    def close():
        nonlocal block
        if block:
            blocks.append(block)
            block = None

    for n, raw in enumerate(lines, 1):
        h = HEADING_RE.match(raw)
        d = BLOCK_RE.match(raw)
        title = h.group(2) if h else ""
        sm = STORY_HEAD_RE.match(title.lstrip("`* ")) if h else None
        if h and not d:
            in_deferred = bool(DEFERRED_HEADING.search(title))
        item = d.group(1) if d else (block["id"] if block else None)
        if CLARIFICATION in raw and ctx.once(("clar", fname, n)):
            ctx.add("unresolved-clarification", "ERROR", fname, n, "unresolved [NEEDS CLARIFICATION marker", item)
        elif DEFER_RE.search(raw) or (in_deferred and not h and re.match(r"^\s*[-*+]\s+\S", raw)):
            _register_deferred(ctx, fname, n, raw.strip(), item, bool(OWNER_RE.search(raw)))
        if h or d or sm:
            level = len(h.group(1)) if h else 0
            kind = rid = None
            if sm and not (d and d.group(1).startswith("US")):
                kind, rid = "US", f"US{sm.group(1)}"
            elif d:
                rid = d.group(1)
                kind = re.match(r"[A-Z]+", rid).group(0)
            if h and kind not in ("US", "T"):
                if level <= story_level:
                    story = None
                if level <= task_level:
                    task = None
            if kind in ("US", "T") and not h:
                kind = None
            if kind:
                close()
                if kind == "US":
                    story, story_level, task = rid, level, None
                if kind == "T" and h:
                    task, task_level = rid, level
                    story = None
                block = {"kind": kind, "id": rid, "line": n, "lines": [(raw, n)], "story": story, "task": task}
                continue
            if h:
                close()
        if block:
            block["lines"].append((raw, n))
    close()
    for b in blocks:
        body = "\n".join(s for s, _ in b["lines"])
        kind, rid = b["kind"], b["id"]
        if kind == "T":
            deps = {x for s, _ in b["lines"] if re.search(r"(?i)depends on|зависит", s) for x in T_RE.findall(s)} - {rid}
            ctx.tasks.append({"id": rid, "file": fname, "line": b["line"], "texts": [(s, ln) for s, ln in b["lines"]],
                              "text": body, "depends": deps})
        elif b["task"]:
            if kind == "TS":
                ctx.task_ts.append(_rec("ts", rid, fname, b["line"], body, story=b["task"]))
        elif not is_spec:
            continue
        elif kind == "US":
            r = _rec("us", rid, fname, b["line"], body, story=rid)
            r.update({"as": [], "ac": [], "ts": [], "accept": ""})
            ctx.stories[rid] = r
        elif kind == "FR":
            ctx.fr[rid] = _rec("fr", rid, fname, b["line"], body)
        elif kind in ("SC", "NFR"):
            ctx.nfr[rid] = _rec("nfr", rid, fname, b["line"], body)
        elif kind in ("AS", "AC", "TS"):
            r = _rec(kind.lower(), rid, fname, b["line"], body, story=b["story"])
            r["test_key"] = False
            {"AS": ctx.as_, "AC": ctx.ac, "TS": ctx.ts}[kind][rid] = r
            s = ctx.stories.get(b["story"])
            if s:
                s[kind.lower()].append(rid)
                s["accept"] += "\n" + body


def _pick(spec_dir: Path, stem: str):
    for ext in (".yaml", ".yml", ".md"):
        p = spec_dir / f"{stem}{ext}"
        if p.is_file():
            return p
    return None


def _load_yaml(ctx, path: Path):
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=_make_loader())
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        msg = str(getattr(e, "problem", None) or e).replace("\n", " ")
        ctx.add("yaml-parse", "ERROR", path.name, mark.line + 1 if mark else None, f"YAML parse error: {msg}")
    return None


def _ids(text, regex, exclude=()):
    return {x for x in regex.findall(text) if x not in exclude}


def _story_lookup_fr(ctx, sid):
    return [f for f in ctx.fr.values() if sid in _ids(f["text"], US_RE)]


def rule_status(ctx, **_):
    if ctx.status_file and (not ctx.status or ctx.status.lower() in ("draft", "черновик")):
        ctx.add("status-draft", "WARN", ctx.status_file, ctx.status_line, f"spec status is {ctx.status or 'empty'}")


def rule_as_without_test(ctx, **_):
    all_ts = list(ctx.ts.values()) + ctx.task_ts
    linked = set()
    for t in all_ts:
        linked |= _ids(t["text"], AC_RE)
    story_explicit = {sid: any(_ids(ctx.ts[t]["text"], AC_RE) for t in s["ts"] if t in ctx.ts) for sid, s in ctx.stories.items()}
    units = list(ctx.as_.values()) + [c for c in ctx.ac.values() if not ctx.stories.get(c["story"], {}).get("as")]
    for r in units:
        story = ctx.stories.get(r["story"])
        explicit = r.get("test_key") or _ids(r["text"], TS_RE, {r["id"]}) or r["id"] in linked
        implicit = bool(story and story["ts"] and not story_explicit.get(r["story"]))
        if not (explicit or implicit):
            what = "acceptance scenario" if r["kind"] == "as" else "acceptance criterion"
            ctx.add("as-without-test", "ERROR", r["file"], r["line"], f"{r['id']}: {what} has no linked test scenario", r["id"])


def rule_story_without_nfr(ctx, **_):
    for sid, s in ctx.stories.items():
        frs = _story_lookup_fr(ctx, sid)
        scope = "\n".join([s["text"], s["accept"]] + [f["main"] for f in frs])
        m = STATE_RE.search(scope)
        if not m:
            continue
        nfr_text = "\n".join(n["text"] for n in ctx.nfr.values() if sid in _ids(n["text"], US_RE))
        hay = (s["accept"] + "\n" + nfr_text).lower()
        found = [c for c, langs in NFR_CATEGORIES.items() if any(k in hay for ks in langs.values() for k in ks)]
        if not found:
            ctx.add("story-without-nfr", "ERROR", s["file"], s["line"],
                    f"{sid}: changes state ('{m.group(0)}') but has no non-functional criterion "
                    f"({'/'.join(NFR_CATEGORIES)})", sid)


def _segment(text, start, end):
    bounds = [m.end() for m in SEGMENT_BREAK.finditer(text) if m.end() <= start]
    s = bounds[-1] if bounds else 0
    e = next((m.start() for m in SEGMENT_BREAK.finditer(text) if m.start() >= end), len(text))
    return s, e


def _decide(text, start, end):
    """'create' | 'modify' | None for the path at text[start:end]."""
    tail = text[end:end + 16]
    if MARKER_NEW.match(tail):
        return "create"
    if MARKER_EXISTING.match(tail):
        return "modify"
    s, e = _segment(text, start, end)
    before = [(m.start(), "create") for m in CREATE_RE.finditer(text[s:start])] + \
             [(m.start(), "modify") for m in MODIFY_RE.finditer(text[s:start])]
    if before:
        return max(before)[1]
    after = [(m.start(), "create") for m in CREATE_RE.finditer(text[end:e])] + \
            [(m.start(), "modify") for m in MODIFY_RE.finditer(text[end:e])]
    after = [a for a in after if a[0] < 60]
    return min(after)[1] if after else None


def _paths(text):
    found = []
    for m in PATH_RE.finditer(text):
        found.append((m.group(1), m.start(1), m.end(1)))
    for m in TICK_RE.finditer(text):
        tok = m.group(1)
        ext = tok.rsplit(".", 1)[-1].lower() if "." in tok else ""
        if "/" not in tok and ext in BARE_EXTENSIONS and not tok.startswith("."):
            found.append((tok, m.start(1), m.end(1)))
        elif tok.startswith(".") and "/" not in tok and ext in BARE_EXTENSIONS | {"gitignore", "env"} and tok != ".env":
            found.append((tok, m.start(1), m.end(1)))
    out = []
    for p, s, e in found:
        norm = p.replace("\\", "/")
        norm = norm[2:] if norm.startswith("./") else norm
        if posixpath.basename(norm).lower() not in NOT_PATHS and not norm.startswith("/") and norm not in (x[0] for x in out):
            out.append((norm, s, e))
    return out


def rule_tasks_paths(ctx, repo: Path, **_):
    occurrences = []
    for t in ctx.tasks:
        for text, line in t["texts"]:
            for p, s, e in _paths(text):
                occurrences.append((t, line, p, _decide(text, s, e)))
    created = {p for _, _, p, d in occurrences if d == "create"}
    for t, line, p, d in occurrences:
        if d == "create" or (repo / p).exists() or p in created:
            continue
        parent = posixpath.dirname(p)
        if parent and not (repo / parent).is_dir() and any(c.startswith(parent + "/") for c in created):
            continue
        if not ctx.once(("path", t["id"], p)):
            continue
        if d == "modify":
            ctx.add("missing-path", "ERROR", t["file"], line, f"{t['id']}: existing file to modify not found under repo: {p}", t["id"])
        else:
            ctx.add("path-unverified", "WARN", t["file"], line,
                    f"{t['id']}: cannot tell whether {p} is new or existing, and it is absent under repo", t["id"])


def rule_tasks_too_long(ctx, max_task_lines: int = 600, **_):
    if ctx.tasks_file and ctx.task_lines > max_task_lines:
        ctx.add("tasks-too-long", "ERROR", ctx.tasks_file, max_task_lines + 1,
                f"tasks file has {ctx.task_lines} lines, limit is {max_task_lines}: split the work", None)


def rule_deferred(ctx, **_):
    for d in ctx.deferred:
        if not d["owner"]:
            ctx.add("deferred-without-owner", "ERROR", d["file"], d["line"],
                    f"deferred item has no owner (person, story, task or issue): {d['text'][:80]}", d["item"])


def rule_task_links(ctx, **_):
    if not ctx.tasks:
        return
    refd = set()
    for t in ctx.tasks:
        links = _ids(t["text"], US_RE) | _ids(t["text"], FR_RE)
        refd |= links
        if not links:
            ctx.add("task-without-requirement", "WARN", t["file"], t["line"],
                    f"{t['id']}: task links to no user story or requirement", t["id"])
    for r in list(ctx.fr.values()) + list(ctx.stories.values()):
        if r["id"] not in refd:
            what = "requirement" if r["kind"] == "fr" else "user story"
            ctx.add("requirement-without-task", "ERROR", r["file"], r["line"], f"{r['id']}: {what} has no task referencing it", r["id"])


def rule_dangling(ctx, **_):
    defined_ts = set(ctx.ts) | {t["id"] for t in ctx.task_ts}
    task_ids = {t["id"] for t in ctx.tasks}

    def check(rec, file, line, item, text, kinds):
        for regex, known, label in kinds:
            for ref in sorted(_ids(text, regex, {item})):
                if ref not in known and ctx.once(("dangle", file, item, ref)):
                    ctx.add("dangling-reference", "ERROR", file, line, f"{item}: references undefined {label} {ref}", item)

    spec_kinds = [(US_RE, set(ctx.stories), "story"), (FR_RE, set(ctx.fr), "requirement")]
    for r in list(ctx.fr.values()) + list(ctx.nfr.values()) + list(ctx.stories.values()):
        check(r, r["file"], r["line"], r["id"], r["text"], spec_kinds + [(AS_RE, set(ctx.as_), "acceptance scenario")])
    for r in ctx.ts.values():
        check(r, r["file"], r["line"], r["id"], r["text"], spec_kinds + [(AS_RE, set(ctx.as_), "acceptance scenario")])
    for r in ctx.as_.values():
        check(r, r["file"], r["line"], r["id"], r["text"], spec_kinds + [(TS_RE, defined_ts, "test scenario")])
    for t in ctx.tasks:
        check(t, t["file"], t["line"], t["id"], t["text"], spec_kinds + [(AS_RE, set(ctx.as_), "acceptance scenario")])
        for dep in sorted(t["depends"]):
            if dep not in task_ids:
                ctx.add("dangling-reference", "ERROR", t["file"], t["line"], f"{t['id']}: depends_on undefined task {dep}", t["id"])
    for r in ctx.task_ts:
        check(r, r["file"], r["line"], r["story"] or r["id"], r["text"], [(AS_RE, set(ctx.as_), "acceptance scenario")])


def rule_ambiguous(ctx, **_):
    for r in list(ctx.fr.values()) + list(ctx.ac.values()) + list(ctx.nfr.values()):
        text = r["main"]
        seen = set()
        for m in AMBIGUOUS_RE.finditer(text):
            word = m.group(1).lower()
            near = text[max(0, m.start() - 30):m.start()] + text[m.end():m.end() + 30]
            if word in seen or re.search(r"\d", near):
                continue
            seen.add(word)
            ctx.add("ambiguous-wording", "WARN", r["file"], r["line"], f"{r['id']}: unquantified wording '{word}'", r["id"])


RULES = (rule_status, rule_as_without_test, rule_story_without_nfr, rule_tasks_paths, rule_tasks_too_long,
         rule_deferred, rule_task_links, rule_dangling, rule_ambiguous)


def lint(spec_dir: Path, repo: Path, max_task_lines: int = 600) -> dict:
    spec_dir, repo = Path(spec_dir), Path(repo)
    if not spec_dir.is_dir():
        raise InputError(f"spec directory not found: {spec_dir}")
    spec_path, tasks_path = _pick(spec_dir, "spec"), _pick(spec_dir, "tasks")
    if spec_path is None:
        raise InputError(f"no spec.yaml / spec.md in {spec_dir}")
    if yaml is None and any(p and p.suffix in (".yaml", ".yml") for p in (spec_path, tasks_path)):
        raise InputError("PyYAML is not installed (pip install pyyaml); it is required for YAML specs")
    ctx = Ctx()
    for path, is_spec in ((spec_path, True), (tasks_path, False)):
        if path is None:
            continue
        try:
            if path.suffix in (".yaml", ".yml"):
                doc = _load_yaml(ctx, path)
                if doc is not None:
                    (_read_spec_yaml if is_spec else _read_tasks_yaml)(ctx, doc, path.name)
            else:
                _read_md(ctx, path.read_text(encoding="utf-8"), path.name, is_spec)
        except Exception as e:
            ctx.unknown(path.name, f"could not read structure: {type(e).__name__}: {e}")
        if not is_spec:
            ctx.tasks_file = path.name
            ctx.task_lines = len(path.read_text(encoding="utf-8").splitlines())
    parse_failed = any(f["rule"] == "yaml-parse" for f in ctx.findings)
    if not parse_failed:
        if not (ctx.stories or ctx.fr or ctx.as_):
            ctx.unknown(spec_path.name, "no user stories, requirements or scenarios recognised")
        if tasks_path is None:
            ctx.unknown(spec_path.name, "no tasks file: task rules skipped")
        elif not ctx.tasks:
            ctx.unknown(tasks_path.name, "no tasks recognised: task rules skipped")
    for rule in RULES:
        try:
            rule(ctx, repo=repo, max_task_lines=max_task_lines)
        except Exception as e:
            ctx.unknown(spec_path.name, f"{rule.__name__} skipped: {type(e).__name__}: {e}")
    errors = [f for f in ctx.findings if f["severity"] == "ERROR"]
    warnings = [f for f in ctx.findings if f["severity"] == "WARN"]
    stats = {"user_stories": len(ctx.stories), "acceptance_scenarios": len(ctx.as_), "test_scenarios": len(ctx.ts),
             "task_test_scenarios": len(ctx.task_ts), "requirements": len(ctx.fr), "tasks": len(ctx.tasks),
             "nfr_entries": len(ctx.nfr), "deferred_items": len(ctx.deferred), "tasks_file_lines": ctx.task_lines}
    return {"ok": not errors, "errors": errors, "warnings": warnings, "stats": stats}


def format_finding(f: dict) -> str:
    where = f"{f['file']}:{f['line']}" if f["line"] else str(f["file"])
    return f"{f['severity']} {f['rule']} {where} {f['message']}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic spec lint before an unattended run.")
    ap.add_argument("spec_dir")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--max-task-lines", type=int, default=600)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        result = lint(Path(args.spec_dir), Path(args.repo), args.max_task_lines)
    except InputError as e:
        print(f"spec_lint: {e}", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for f in result["errors"] + result["warnings"]:
            print(format_finding(f))
        print(f"spec_lint: {len(result['errors'])} error(s), {len(result['warnings'])} warning(s)")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
