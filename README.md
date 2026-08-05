# WAF Current State Scripts

Automated current-state assessment against the [Databricks Well-Architected
Framework](https://docs.databricks.com/en/lakehouse-architecture/index.html), scored
using the question bank published by the
[WAF Assessment Tool](https://databricks-solutions.github.io/waf-assessment-tool/)
(**151 questions across 7 pillars**).

Each pillar gets its own notebook. Every question is answered from observable evidence
(Unity Catalog system tables, the Databricks SDK, and workspace APIs) or is explicitly
marked `manual-review` when it depends on process or documentation that telemetry cannot
see. A driver notebook runs all pillars and combines them into one exportable report.

> **Status: work in progress.** 2 of 7 pillar check modules are complete (Data & AI
> Governance, Reliability). See [Build status](#build-status) below.

## Status vocabulary

Statuses match the WAF Assessment Tool so results can be transcribed straight back into it:

| Status | Meaning |
| --- | --- |
| `open` | No supporting evidence found, or the control is absent |
| `in-progress` | Partially adopted — below the completion threshold |
| `completed` | Adoption at or above the completion threshold (default 80%) |
| `manual-review` | Not observable from telemetry; needs a human answer |

`manual-review` questions are excluded from the automated maturity score, because
scoring them would misrepresent questions nobody has answered yet.

### How a question gets scored

Coverage questions are graded on a ratio with an auto-generated rationale. For example
`DG-01-02` (*Manage metadata for all data assets in one place*) measures the share of
production tables carrying a maintained table-level description:

```
DG-01-02  [in-progress]  Manage metadata for all data assets in one place
    Reason: 38.0% of production (1 catalog(s): samples) tables are registered in
            Unity Catalog with a table-level description maintained (35 of 92) -
            below the 80.0% threshold. Ownership is set on 100.0% of them.
```

Rules applied consistently across pillars:

- **Nothing found is `open`, not a pass.** An empty denominator means no evidence of
  adoption, so it never scores as complete.
- **0 of N is `in-progress`, not `open`.** If assets exist but none are covered, that is
  a measured 0% rather than an absence of evidence.
- **`>= 80%` is `completed`; below is `in-progress`.** Configurable via `complete_at`.
- **The percentage always appears in the rationale**, along with the raw counts and the
  threshold that decided the status.

## Production scope

Governance matters most in production, so catalog-level questions are scored against
production by default. Scope is config-driven:

```python
PROD_CATALOG_PATTERNS        = ["prod%", "main", "%_prod", "%_production"]
PROD_SCHEMA_EXCLUDE_PATTERNS = ["%dev%", "%test%", "%sandbox%", "%tmp%", "%scratch%"]
```

If no catalog matches, the notebooks fall back to **all non-system catalogs** and prefix
every affected rationale with a loud caveat so a wide scope is never silently assumed:

```
[SCOPE: no catalog matched the configured production patterns, so ALL non-system
catalogs were assessed] 38.0% of ...
```

## Repository layout

```
waf_core.py              # scoring, scope resolution, persistence, report rendering
waf_questions.py         # generated: all 151 questions (id, pillar, principle, title)
checks/
  governance.py          # DG-*  (12)  complete
  reliability.py         # R-*   (19)  complete
tools/
  gen_questions.py       # regenerate waf_questions.py from the published question bank
  smoke_test.py          # run every check against a real workspace from your laptop
tests/
  test_waf_core.py       # 37 offline unit tests for scoring and reporting
```

## Build status

| Pillar | Questions | Check module | Notebook |
| --- | ---: | --- | --- |
| Data & AI Governance | 12 | done | pending |
| Reliability | 19 | done | pending |
| Security, Compliance & Privacy | 38 | pending | pending |
| Operational Excellence | 26 | pending | pending |
| Performance Efficiency | 23 | pending | pending |
| Cost Optimization | 19 | pending | pending |
| Interoperability & Usability | 14 | pending | pending |

Still to come: the seven pillar notebooks, the config notebook, and the driver notebook
that runs all pillars and emits the combined report.

## Testing

Offline unit tests cover the scoring thresholds, scope SQL generation (including quote
escaping), and report rendering — no workspace required:

```bash
python -m unittest discover -s tests -v
```

The smoke test executes every check's SQL against a real workspace via the Databricks
CLI, so query syntax and permissions are verified without a cluster. Statuses it prints
reflect the test workspace, not a customer, so only errors matter:

```bash
python tools/smoke_test.py --profile <PROFILE> --pillar governance
```

Both currently pass: 37/37 unit tests, and all governance + reliability checks execute
cleanly with 0 SQL failures.

## Requirements

- Unity Catalog, with the assessor granted `SELECT` on the `system` catalog schemas
  (`system.access`, `system.billing`, `system.compute`, `system.lakeflow`,
  `system.query`, `system.storage`, `system.serving`, `system.mlflow`).
- System tables enabled. Checks degrade gracefully to `open` with an explanatory
  rationale when a schema is unavailable, rather than failing the run.
- Reads only. Nothing in the assessment path writes to customer data; the only writes
  are the assessment's own results table.
