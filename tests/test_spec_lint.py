"""spec_lint: one test per rule on invented fixtures, Markdown form, CLI exit codes, robustness."""

import json
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import spec_lint  # noqa: E402

FIXTURES = HERE / "fixtures" / "spec_lint"

pytestmark = pytest.mark.skipif(spec_lint.yaml is None, reason="PyYAML not installed")


@pytest.fixture
def work(tmp_path: Path):
    spec = tmp_path / "spec"
    shutil.copytree(FIXTURES / "clean", spec)
    shutil.copytree(FIXTURES / "repo", tmp_path / "repo")
    return spec, tmp_path / "repo"


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def append(path: Path, text: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def rules(result: dict, severity: str) -> list[str]:
    return [f["rule"] for f in result["errors" if severity == "ERROR" else "warnings"]]


def run(spec: Path, repo: Path, **kw) -> dict:
    return spec_lint.lint(spec, repo, **kw)


def test_clean_spec_has_no_findings(work):
    result = run(*work)
    assert result["errors"] == [] and result["warnings"] == []
    assert result["ok"] is True
    assert result["stats"]["acceptance_scenarios"] == 3 and result["stats"]["test_scenarios"] == 3


def test_as_without_test(work):
    spec, repo = work
    edit(spec / "spec.yaml", '    acceptance_criteria:\n      - id: "AC-1"',
         '      - id: "AS-9"\n        given: "x"\n        when: "y"\n        then: "z"\n    acceptance_criteria:\n      - id: "AC-1"')
    result = run(spec, repo)
    hits = [f for f in result["errors"] if f["rule"] == "as-without-test"]
    assert [f["item"] for f in hits] == ["AS-9"] and hits[0]["file"] == "spec.yaml" and hits[0]["line"]


def test_as_linked_implicitly_by_story_level_ts(work):
    spec, repo = work
    edit(spec / "spec.yaml", '        covers: ["AS-1"]\n', "")
    edit(spec / "spec.yaml", '        covers: ["AS-2"]\n', "")
    assert "as-without-test" not in rules(run(spec, repo), "ERROR")


def test_story_without_test_scenarios_flags_its_as(work):
    spec, repo = work
    text = (spec / "spec.yaml").read_text(encoding="utf-8")
    start = text.index("    test_scenarios:\n      - id: \"TS-3\"")
    end = text.index("functional_requirements:")
    (spec / "spec.yaml").write_text(text[:start] + "\n" + text[end:], encoding="utf-8")
    hits = [f["item"] for f in run(spec, repo)["errors"] if f["rule"] == "as-without-test"]
    assert hits == ["AS-3"]


def test_state_changing_story_without_nfr(work):
    spec, repo = work
    edit(spec / "spec.yaml", "Saving the same note twice is idempotent and never duplicates data.", "Saving works.")
    hits = [f for f in run(spec, repo)["errors"] if f["rule"] == "story-without-nfr"]
    assert [f["item"] for f in hits] == ["US1"]


def test_story_nfr_from_spec_level_criterion(work):
    spec, repo = work
    edit(spec / "spec.yaml", "Saving the same note twice is idempotent and never duplicates data.", "Saving works.")
    edit(spec / "spec.yaml", 'description: "A ledger with 1000 notes lists in under 200 ms."\n    story_refs: ["US2"]',
         'description: "Saving survives a restart without data loss, retry is safe."\n    story_refs: ["US1"]')
    assert "story-without-nfr" not in rules(run(spec, repo), "ERROR")


def test_russian_state_verb_and_category(tmp_path: Path):
    (tmp_path / "spec.yaml").write_text(
        'status: "Approved"\nuser_stories:\n  - id: "US1"\n    title: "Оплата"\n'
        '    description: "Пользователь оплачивает заказ."\n    acceptance_scenarios:\n'
        '      - {id: "AS-1", given: "a", when: "b", then: "c"}\n    test_scenarios:\n      - {id: "TS-1", when: "x"}\n',
        encoding="utf-8")
    assert "story-without-nfr" in rules(run(tmp_path, tmp_path), "ERROR")
    edit(tmp_path / "spec.yaml", '"c"}', '"повторная оплата идемпотентна"}')
    assert "story-without-nfr" not in rules(run(tmp_path, tmp_path), "ERROR")


def test_missing_path(work):
    spec, repo = work
    edit(spec / "tasks.yaml", "update src/cli.py", "update src/gone.py")
    hits = [f for f in run(spec, repo)["errors"] if f["rule"] == "missing-path"]
    assert len(hits) == 1 and "src/gone.py" in hits[0]["message"] and hits[0]["item"] == "T002"


def test_created_path_is_not_missing(work):
    spec, repo = work
    assert not [f for f in run(spec, repo)["errors"] if f["rule"] == "missing-path"]


def test_undecidable_path_is_warning(work):
    spec, repo = work
    edit(spec / "tasks.yaml", 'detail: "Print entries', 'detail: "Cases live in tests/test_list.py. Print entries')
    result = run(spec, repo)
    assert "path-unverified" in rules(result, "WARN") and "missing-path" not in rules(result, "ERROR")


def test_tasks_too_long(work):
    spec, repo = work
    assert "tasks-too-long" not in rules(run(spec, repo), "ERROR")
    assert "tasks-too-long" in rules(run(spec, repo, max_task_lines=5), "ERROR")


def test_deferred_without_owner(work):
    spec, repo = work
    append(spec / "spec.yaml", '\ndeferred:\n  - "Export to JSON"\n')
    hits = [f for f in run(spec, repo)["errors"] if f["rule"] == "deferred-without-owner"]
    assert len(hits) == 1 and "Export to JSON" in hits[0]["message"]


def test_marker_without_owner_and_with_owner(work):
    spec, repo = work
    edit(spec / "spec.yaml", "never duplicates data.", "never duplicates data. TODO decide the file format.")
    assert "deferred-without-owner" in rules(run(spec, repo), "ERROR")
    edit(spec / "spec.yaml", "decide the file format.", "decide the file format (US2).")
    assert "deferred-without-owner" not in rules(run(spec, repo), "ERROR")


def test_lowercase_todo_inside_a_name_is_not_a_marker(work):
    spec, repo = work
    edit(spec / "spec.yaml", "never duplicates data.", "never duplicates data; seed fixture ts-todo is reused.")
    assert "deferred-without-owner" not in rules(run(spec, repo), "ERROR")


def test_unresolved_clarification(work):
    spec, repo = work
    edit(spec / "spec.yaml", "never duplicates data.", "never duplicates data. [NEEDS CLARIFICATION: which file?]")
    result = run(spec, repo)
    assert "unresolved-clarification" in rules(result, "ERROR")
    assert "deferred-without-owner" not in rules(result, "ERROR")


def test_dangling_references(work):
    spec, repo = work
    edit(spec / "tasks.yaml", '["FR-001", "FR-002"]', '["FR-001", "FR-002", "FR-099"]')
    edit(spec / "tasks.yaml", 'depends_on: ["T001"]', 'depends_on: ["T009"]')
    edit(spec / "spec.yaml", 'covers: ["AS-1"]', 'covers: ["AS-77"]')
    messages = " | ".join(f["message"] for f in run(spec, repo)["errors"] if f["rule"] == "dangling-reference")
    assert "FR-099" in messages and "T009" in messages and "AS-77" in messages


def test_requirement_without_task(work):
    spec, repo = work
    edit(spec / "spec.yaml", "functional_requirements:\n",
         'functional_requirements:\n  - id: "FR-004"\n    text: "The tool MUST reject empty titles."\n    story_refs: ["US1"]\n')
    hits = [f["item"] for f in run(spec, repo)["errors"] if f["rule"] == "requirement-without-task"]
    assert hits == ["FR-004"]


def test_story_without_task(work):
    spec, repo = work
    edit(spec / "tasks.yaml", 'story_ref: "US2"\n    requirement_refs: ["FR-003"]', 'story_ref: "US1"\n    requirement_refs: ["FR-001"]')
    hits = {f["item"] for f in run(spec, repo)["errors"] if f["rule"] == "requirement-without-task"}
    assert {"US2", "FR-003"} <= hits


def test_task_without_requirement_warns(work):
    spec, repo = work
    edit(spec / "tasks.yaml", '    story_ref: "US2"\n    requirement_refs: ["FR-003"]\n', "")
    result = run(spec, repo)
    assert [f["item"] for f in result["warnings"] if f["rule"] == "task-without-requirement"] == ["T002"]


def test_ambiguous_wording_warns_and_quantified_is_fine(work):
    spec, repo = work
    edit(spec / "spec.yaml", "MUST print notes newest first.", "MUST print notes quickly.")
    result = run(spec, repo)
    assert "ambiguous-wording" in rules(result, "WARN") and result["ok"]
    edit(spec / "spec.yaml", "print notes quickly.", "print notes quickly (under 50 ms).")
    assert "ambiguous-wording" not in rules(run(spec, repo), "WARN")


def test_status_draft_and_empty_warn(work):
    spec, repo = work
    edit(spec / "spec.yaml", 'status: "Approved"', 'status: "Draft"')
    result = run(spec, repo)
    assert rules(result, "WARN") == ["status-draft"] and result["ok"]
    edit(spec / "spec.yaml", 'status: "Draft"', 'status: ""')
    assert rules(run(spec, repo), "WARN") == ["status-draft"]


def test_yaml_parse_error_has_line(work):
    spec, repo = work
    (spec / "tasks.yaml").write_text('feature: "x"\ntasks:\n  - id: "T001"\n    title: [unclosed\n', encoding="utf-8")
    result = run(spec, repo)
    hits = [f for f in result["errors"] if f["rule"] == "yaml-parse"]
    assert len(hits) == 1 and hits[0]["file"] == "tasks.yaml" and hits[0]["line"] >= 4 and not result["ok"]


def test_markdown_spec_as_without_test():
    result = spec_lint.lint(FIXTURES / "markdown", FIXTURES / "repo")
    assert [f["item"] for f in result["errors"]] == ["AS-2"]
    assert result["errors"][0]["rule"] == "as-without-test" and result["errors"][0]["file"] == "spec.md"
    assert result["stats"]["acceptance_scenarios"] == 2 and result["stats"]["requirements"] == 2
    assert result["stats"]["tasks"] == 1


def test_markdown_clarification_and_dangling(tmp_path: Path):
    shutil.copytree(FIXTURES / "markdown", tmp_path / "m")
    append(tmp_path / "m" / "spec.md", "- **FR-003**: The tool MUST rotate files [NEEDS CLARIFICATION: when?] (US7).\n")
    found = rules(spec_lint.lint(tmp_path / "m", FIXTURES / "repo"), "ERROR")
    assert "unresolved-clarification" in found and "dangling-reference" in found


def test_unknown_structure_does_not_crash(tmp_path: Path):
    (tmp_path / "spec.yaml").write_text(
        'title: x\nuser_stories: "not a list"\nfunctional_requirements: {a: 1}\nsections:\n  - foo\n', encoding="utf-8")
    (tmp_path / "tasks.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    result = spec_lint.lint(tmp_path, tmp_path)
    assert "unknown-structure" in rules(result, "WARN") and result["ok"]


def test_scalar_items_do_not_crash(tmp_path: Path):
    (tmp_path / "spec.yaml").write_text(
        'status: Approved\nuser_stories:\n  - "a string story"\n  - id: US1\n    acceptance_scenarios: 5\n'
        'functional_requirements:\n  - id: FR-001\n    text: 7\n', encoding="utf-8")
    (tmp_path / "tasks.yaml").write_text("tasks:\n  - id: T001\n    steps: 3\n    depends_on: null\n", encoding="utf-8")
    assert "unknown-structure" in rules(spec_lint.lint(tmp_path, tmp_path), "WARN")


def test_input_errors(tmp_path: Path):
    with pytest.raises(spec_lint.InputError):
        spec_lint.lint(tmp_path / "nope", tmp_path)
    with pytest.raises(spec_lint.InputError):
        spec_lint.lint(tmp_path, tmp_path)


def test_yaml_missing_is_input_error_but_markdown_works(monkeypatch, work):
    spec, repo = work
    monkeypatch.setattr(spec_lint, "yaml", None)
    with pytest.raises(spec_lint.InputError, match="PyYAML"):
        spec_lint.lint(spec, repo)
    assert spec_lint.lint(FIXTURES / "markdown", repo)["stats"]["acceptance_scenarios"] == 2


def test_cli_exit_codes_and_output(work, capsys):
    spec, repo = work
    assert spec_lint.main([str(spec), "--repo", str(repo)]) == 0
    capsys.readouterr()
    assert spec_lint.main([str(spec), "--repo", str(repo), "--max-task-lines", "5"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("ERROR tasks-too-long tasks.yaml:6 ")
    assert spec_lint.main([str(spec.parent / "missing")]) == 3
    assert "spec directory not found" in capsys.readouterr().err
    edit(spec / "spec.yaml", 'status: "Approved"', 'status: "Draft"')
    assert spec_lint.main([str(spec), "--repo", str(repo)]) == 0
    assert "WARN status-draft spec.yaml:2 " in capsys.readouterr().out


def test_cli_json_shape(work, capsys):
    spec, repo = work
    code = spec_lint.main([str(spec), "--repo", str(repo), "--max-task-lines", "5", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == 1 and data["ok"] is False
    assert set(data) == {"ok", "errors", "warnings", "stats"}
    assert set(data["errors"][0]) == {"rule", "severity", "file", "line", "message", "item"}
    assert data["errors"][0]["severity"] == "ERROR" and data["stats"]["acceptance_scenarios"] == 3
