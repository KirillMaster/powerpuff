"""spec_slice - the part of a shared spec that one queue unit needs, instead of the whole spec in every prompt.

yamlkit directory (spec.yaml / tasks.yaml / contracts.yaml / data-model.yaml): the items whose `id` the unit names
(`requirements`, or ids such as FR-1, AS-2, T012, C-3, E-4 in the unit text), the tasks that reference the unit's
requirements, and the contracts / entities those tasks reference. Markdown files: the paragraphs that mention those
ids. A small spec (<= `limit` characters) is inlined whole. Every source file is listed by path for Read on demand.
"""

import re
from pathlib import Path

YAML_FILES = ("spec.yaml", "tasks.yaml", "contracts.yaml", "data-model.yaml")
MD_FILES = ("spec.md", "plan.md", "tasks.md", "data-model.md", "research.md")
POINTER_FILES = ("spec.yaml", "spec.md", "plan.yaml", "plan.md", "tasks.yaml", "tasks.md", "contracts.yaml",
                 "data-model.yaml", "data-model.md", "research.md", "quickstart.md")
ID_IN_TEXT = re.compile(r"\b(?:FR|AS|AC|SC|NFR|C|E)-\d+\b|\bT\d{3,}\b|\bUS\d+\b")
MAX_TASKS = 12


def rel(path: Path, repo: Path) -> str:
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def pick(node: object, want: set[str]):
    """Dicts whose `id` is wanted, outermost first; the children of a picked item are not visited again."""
    if isinstance(node, dict):
        if str(node.get("id", "")) in want:
            yield node
            return
        for v in node.values():
            yield from pick(v, want)
    elif isinstance(node, list):
        for v in node:
            yield from pick(v, want)


def walk(node: object):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def dump(items: list) -> str:
    import yaml
    return yaml.safe_dump(items, allow_unicode=True, sort_keys=False, width=120).strip()


def load_yaml(path: Path) -> object:
    import yaml
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None


def refs(item: dict, *keys: str) -> set[str]:
    out: set[str] = set()
    for k in keys:
        v = item.get(k)
        if isinstance(v, list):
            out |= {str(x) for x in v}
        elif isinstance(v, str) and v:
            out.add(v)
    return out


def flatten_ids(task: dict) -> dict:
    """Task-local ids (`acceptance_criteria: [{id: AC-1}]` repeats in every task) become `AC-1: text` lines, so the
    plain engine's coverage check does not take them for the unit's requirements."""
    out = dict(task)
    for k, v in task.items():
        if isinstance(v, list) and v and all(isinstance(x, dict) and "id" in x for x in v):
            out[k] = [f"{x['id']}: " + "; ".join(f"{kk}={vv}" for kk, vv in x.items() if kk != "id") for x in v]
    return out


def yamlkit_sections(d: Path, want: set[str], reqs: set[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    spec = load_yaml(d / "spec.yaml") if (d / "spec.yaml").is_file() else None
    if spec is not None:
        local = re.compile(r"^(?:AC|AS|TS)-\d+$")  # repeated under every requirement: only the unit's own count
        items = list(pick(spec, {w for w in want if w in reqs or not local.match(w)}))
        if items:
            out.append(("spec.yaml: the unit's requirements", dump(items)))
    tasks_doc = load_yaml(d / "tasks.yaml") if (d / "tasks.yaml").is_file() else None
    linked: set[str] = set()
    if isinstance(tasks_doc, dict) and isinstance(tasks_doc.get("tasks"), list):
        tasks = [t for t in tasks_doc["tasks"] if isinstance(t, dict)
                 and (str(t.get("id", "")) in want or refs(t, "requirement_refs") & reqs)]
        named = [t for t in tasks if str(t.get("id", "")) in want]
        tasks = [flatten_ids(t) for t in (named + [t for t in tasks if t not in named])[:MAX_TASKS]]
        for t in tasks:
            linked |= refs(t, "entity_refs", "contract_refs")
        if tasks:
            out.append(("tasks.yaml: tasks of these requirements", dump(tasks)))
    for name, key in (("contracts.yaml", "contracts"), ("data-model.yaml", "entities")):
        doc = load_yaml(d / name) if (d / name).is_file() else None
        if isinstance(doc, dict) and isinstance(doc.get(key), list):
            items = [x for x in doc[key] if isinstance(x, dict) and str(x.get("id", "")) in (want | linked)]
            if items:
                out.append((f"{name}: referenced {key}", dump(items)))
    return out


def md_sections(d: Path, want: set[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pat = re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(w) for w in sorted(want)) + r")(?![\w])") if want else None
    for name in MD_FILES:
        p = d / name
        if not pat or not p.is_file():
            continue
        blocks = re.split(r"\n\s*\n", p.read_text(encoding="utf-8"))
        hits = [b.strip() for b in blocks if pat.search(b)]
        if hits:
            out.append((f"{name}: paragraphs naming the unit's ids", "\n\n".join(hits)))
    return out


def trim(sections: list[tuple[str, str]], limit: int) -> list[str]:
    parts: list[str] = []
    left = limit
    for title, body in sections:
        if left <= 200:
            parts.append(f"### {title}\n(omitted: size limit; read the source file)")
            continue
        if len(body) > left:
            body = body[:left].rsplit("\n", 1)[0] + "\n... (truncated; read the source file)"
        parts.append(f"### {title}\n\n{body}")
        left -= len(body)
    return parts


def build(shared: Path, unit: dict, repo: Path, limit: int = 12000) -> str:
    """Markdown block `## Shared spec ...` for the unit spec file."""
    head = "## Shared spec (context only; build just the unit above)"
    if shared.is_file():
        text = shared.read_text(encoding="utf-8")
        if len(text) <= limit:
            return f"{head}\n\n{text.strip()}\n"
        return f"{head}\n\nThe whole spec is `{rel(shared, repo)}` ({len(text)} chars): read only what the unit needs.\n"
    files = [f"- `{rel(shared / n, repo)}`" for n in POINTER_FILES if (shared / n).is_file()]
    files += [f"- `{rel(p, repo)}/`" for p in sorted(shared.iterdir()) if p.is_dir() and not p.name.startswith(".")]
    whole = sum((shared / n).stat().st_size for n in POINTER_FILES if (shared / n).is_file())
    if whole <= limit:
        text = "\n\n".join(f"# {n}\n\n{(shared / n).read_text(encoding='utf-8')}" for n in POINTER_FILES
                           if (shared / n).is_file())
        return f"{head}\n\n{text.strip()}\n"
    reqs = {str(r) for r in unit.get("requirements") or []}
    want = reqs | set(ID_IN_TEXT.findall(str(unit.get("spec") or "")))
    try:
        sections = yamlkit_sections(shared, want, reqs)
    except ImportError:
        sections = []
    if not sections:
        sections = md_sections(shared, want)
    lines = [head, "", "The full spec is too large to inline. Below: only the items this unit references. Read the "
             "source files for anything else you need (never implement other units):", *files, ""]
    lines += trim(sections, limit) or ["(no items of the spec reference this unit's ids; rely on the unit spec)"]
    return "\n".join(lines) + "\n"
