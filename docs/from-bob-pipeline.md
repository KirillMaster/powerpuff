# What carries over from bob-pipeline and what does not

Criterion: keep what gave measurable benefit on the benchmark or in project history and does not use a model where a script is enough.

| From bob-pipeline | Decision | Rationale |
|---|---|---|
| Mechanical gates (format, linter, architecture, tests) as the "done" condition | keep: `forge_check.py` | deterministic, cheap |
| Project config `.claude/bob-pipeline.yaml` (test command, enabled tools) | not read: the file is only mentioned in the audit prompt as a hint for drafting gates when `powerpuff.yaml` is absent; the source of gates is the `powerpuff.yaml` profile | a human approves the profile, the model does not choose gates |
| Resuming after a failure and waiting out the subscription limit, cumulative budget | keep: `forge_run.py` | autonomous run without a human |
| Ban on weakening gates and touching tests | keep: frozen hashes, restore from git | otherwise the model fits the tests to its code |
| Independent check of requirement coverage | keep as a single acceptance pass by a strong model in a fresh context | one check instead of five roles |
| Requirement → test traceability | keep: `R<n>` identifiers in acceptance tests | checked by a script |
| Gates K2–K4 (red test on a clean base, rollback classification, public API diff), `ci_gates.py` | candidates: port one at a time, only with a measured benefit | not measured separately on the benchmark |
| Run metrics | simplify: cost by model and stage in `state.json` | enough for the report |
| Five roles (Specifier, Coder, Architect, Hardener, QA) on every slice | do not keep | pilot: quality 0.70–0.78 vs 0.94–1.0 for plain at 5–20 times the cost |
| Gherkin and QA procedures as a mandatory layer | do not keep | duplicates the yamlkit spec, showed no benefit |
| Mutation testing in the loop | not kept by default | no data on benefit in an agent loop; expensive |
| Orchestrator model | do not keep | $11.42 vs ~$1 |
