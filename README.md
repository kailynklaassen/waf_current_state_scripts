# WAF Current State Scripts

Automated current-state assessment against the [Databricks Well-Architected
Framework](https://docs.databricks.com/en/lakehouse-architecture/index.html), scored
using the question bank published by the
[WAF Assessment Tool](https://databricks-solutions.github.io/waf-assessment-tool/)
(**151 questions across 7 pillars**).

Each pillar is its own notebook. Every question is either answered from observable
evidence — Unity Catalog system tables, the Databricks SDK, workspace APIs — or
explicitly marked `manual-review` when it depends on process or documentation that
telemetry cannot see. A driver notebook runs all pillars and combines them into one
exportable report.

**113 of 151 questions are automated today** across 6 pillars. Security, Compliance &
Privacy (38 questions) is not implemented — see [Coverage](#coverage).

## Quick start

1. Clone this repo into a Databricks **Git folder** (Workspace → Repos → Add repo).
2. Open `notebooks/00_config.ipynb`, set `CATALOG` and `SCHEMA`, and review
   `PROD_CATALOG_PATTERNS`. Nothing runs until `CATALOG`/`SCHEMA` are filled in.
3. Run `notebooks/99_run_all.ipynb`.

That produces the combined report inline, a markdown file, a JSON export (in the WAF
Assessment Tool's format), a CSV, and a results table. To assess a single pillar, run that
pillar's notebook instead — each one `%run`s the config itself.

Requires serverless or a cluster on DBR 13.3 LTS or newer (the checks use
`count_if`, `try_cast`, and lambda expressions).

### Where the exports are saved

By default, section **7. Export** in `99_run_all` saves the markdown, JSON, and CSV to a
Unity Catalog Volume so they persist and can be governed and shared. The cell creates the
Volume if it doesn't exist:

```sql
CREATE VOLUME IF NOT EXISTS <catalog>.<schema>.waf_exports;
```

and writes the three files to `/Volumes/<catalog>/<schema>/waf_exports/` (using the
`CATALOG`/`SCHEMA` from `00_config`).

To change this, edit the top of the export cell:

- **`VOLUME_NAME = "waf_exports"`** — rename the Volume the exports are written to.
- **`USE_VOLUME = True`** — set to `False` to write to `/tmp` on the driver instead
  (ephemeral, but downloadable from the UI).

If the Volume can't be created (for example, missing privileges on the catalog/schema), the
cell falls back to `/tmp` and prints a note rather than failing the run.

## Status vocabulary

Statuses match the WAF Assessment Tool, so results transcribe straight back into it:

| Status | Meaning |
| --- | --- |
| `open` | No supporting evidence found, or the control is absent |
| `in-progress` | Partially adopted — below the completion threshold |
| `completed` | Adoption at or above the completion threshold (default 80%) |
| `manual-review` | Not observable from telemetry; needs a human answer |

`manual-review` questions are **excluded from the automated score** — scoring questions
nobody has answered yet would misrepresent the result.

### How a question gets scored

Coverage questions are graded on a ratio, with the rationale generated from the same
criteria that decided the status. For `DG-01-02` (*Manage metadata for all data assets in
one place*), that means the share of production tables carrying a maintained description:

```
DG-01-02  [in-progress]  Manage metadata for all data assets in one place
    Reason: 38.0% of production (1 catalog(s): prod_sales) tables are registered in
            Unity Catalog with a table-level description maintained (35 of 92) -
            below the 80.0% threshold. Ownership is set on 100.0% of them.
```

Rules applied consistently across all pillars:

- **Nothing found is `open`, not a pass.** An empty denominator means no evidence of
  adoption, so it never scores `completed`.
- **0 of N is `open`, not `in-progress`.** A measured zero is no adoption of the control,
  so it is an open item. `in-progress` is reserved for genuine partial adoption — strictly
  **more than 0** but below the threshold. A control whose capability exists but is used
  zero times (collected-but-never-reviewed, defined-but-never-exercised) is `open` for the
  same reason.
- **`>= 80%` is `completed`, below (but above 0) is `in-progress`.** Tunable via `COMPLETE_AT`. A few
  checks use a deliberately lower bar where 80% would be wrong (for example file pruning
  at 50%, or declarative-pipeline adoption at 30%); those state their threshold in the
  rationale.
- **The percentage is always in the rationale**, with raw counts and the deciding
  threshold.
- **A check that errors reports `open`** with the error text, rather than aborting the
  pillar or silently passing.
- **Unreadable evidence never scores `completed`.** A check distinguishes "we looked and
  found nothing" from "we could not look" — the latter is `open` with a rationale naming
  the unreadable source. A permanent test enforces this, because reporting a control as
  satisfied when it was never checked is the worst failure this tool could have.

### Questions that can't be automated

Some questions are genuinely unanswerable from a workspace — `DG-01-01` ("Establish data
governance process") is a process question, not a setting. These return `manual-review`
naming the evidence to collect, which doubles as an interview checklist:

```
DG-01-01  [manual-review]  Establish data governance process
    Reason: Not determinable from system tables or APIs: a governance operating model
            is an organizational process, not a platform setting. Gather from your
            Databricks environment's owners: the data governance charter, named data
            owners/stewards, and the policy review cadence.
```

There are 6 such questions across the implemented pillars: `DG-01-01`, `DG-03-01`,
`OE-01-01`, `OE-01-09`, `OE-03-03`, and `PE-03-01`.

## Production scope

Governance matters most in production, so catalog-level questions are scored against
production by default. Scope is config-driven:

```python
PROD_CATALOG_PATTERNS        = ["prod%", "main", "%_prod", "%_production"]
PROD_SCHEMA_EXCLUDE_PATTERNS = ["%dev%", "%test%", "%sandbox%", "%tmp%", "%scratch%"]
SYSTEM_CATALOGS              = ["system", "__databricks_internal%", "samples"]
```

If no catalog matches, the notebooks fall back to **all non-system catalogs** and prefix
every affected rationale with a caveat, so a wide scope is never silently assumed:

```
[SCOPE: no catalog matched the configured production patterns, so ALL non-system
catalogs were assessed] 38.0% of ...
```

`00_config` prints the resolved scope and warns when the fallback is in effect.

## Coverage

| Pillar | Questions | Automated | `manual-review` |
| --- | ---: | ---: | ---: |
| Data & AI Governance | 12 | 10 | 2 |
| Interoperability & Usability | 14 | 14 | 0 |
| Operational Excellence | 26 | 23 | 3 |
| Reliability | 19 | 19 | 0 |
| Performance Efficiency | 23 | 22 | 1 |
| Cost Optimization | 19 | 19 | 0 |
| **Subtotal** | **113** | **107** | **6** |
| Security, Compliance & Privacy | 38 | — | not implemented |
| **Total** | **151** | | |

Security is excluded deliberately rather than half-answered. Most of its 38 questions are
account-console, identity-provider, or cloud-network settings (SSO, MFA, customer-managed
keys, PrivateLink, VPC configuration) that a workspace-scoped script cannot observe, and
guessing at them would produce confident, wrong answers about your Databricks
environment's security posture. `99_run_all` names the pillar as unimplemented instead
of quietly dropping it.

For the Security, Compliance & Privacy pillar, run the
[**Security Analysis Tool (SAT)**](https://databricks-industry-solutions.github.io/security-analysis-tool/docs/motivation/)
instead. SAT is a Databricks solution accelerator that checks your workspace and account
configuration against Databricks security best practices and produces a scored dashboard —
exactly the account-console, identity, and network settings this tool cannot see. Use SAT
for that pillar and transcribe its findings back into the WAF Assessment Tool.

## Repository layout

```
notebooks/
  00_config.ipynb                 configuration, validation, scope, run_pillar()
  01_governance.ipynb             DG-*   12 questions
  02_interoperability.ipynb       IU-*   14
  03_operational_excellence.ipynb OE-*   26
  04_reliability.ipynb            R-*    19
  05_performance.ipynb            PE-*   23
  06_cost.ipynb                   CO-*   19
  99_run_all.ipynb                driver: all pillars + combined report + export
checks/                           one module per pillar; pure functions, no Spark at import
waf_core.py                       scoring, scope resolution, persistence, report rendering
waf_questions.py                  generated: all 151 questions (id, pillar, principle, title)
tools/
  gen_questions.py                regenerate waf_questions.py from the published bank
  smoke_test.py                   run every check against a real workspace, no cluster
  nbutil.py                       build and validate notebooks
tests/
  test_waf_core.py                scoring, scope SQL, report rendering (44 tests)
  test_notebooks.py               notebook structure and pillar coverage (26 tests)
  test_greenfield.py              every check on an empty/locked-down workspace (8 tests)
```

## Results table and combining pillars

Each pillar writes to `<CATALOG>.<SCHEMA>.waf_assessment_results`, keyed by `run_id`, so
pillars can be run independently — even in separate sessions — and still combine into one
report. Re-running a pillar replaces only that pillar's rows for that `run_id`.

```sql
-- Current state by pillar
SELECT pillar, status, count(*) AS questions
FROM <catalog>.<schema>.waf_assessment_results
WHERE run_id = '<run_id>'
GROUP BY pillar, status ORDER BY pillar, status;

-- Track one question across runs to show progress
SELECT run_id, run_timestamp, status, reason
FROM <catalog>.<schema>.waf_assessment_results
WHERE question_id = 'DG-01-02' ORDER BY run_timestamp DESC;
```

Set `PERSIST_RESULTS = False` to run fully read-only; the report is then built from
in-memory results for the current session only.

### Lineage snapshot

Several checks read `system.access.table_lineage` / `column_lineage`, which are large and
would otherwise be re-scanned by each check. Instead, `00_config` materializes lineage once,
up front, into the configured schema, following the pattern in
[databricks_lineage](https://github.com/kailynklaassen/databricks_lineage):

- **`waf_lineage_edges`** — deduped direct table-lineage edges (lookback-windowed), projected
  to the columns the checks read.
- **`waf_lineage_closure`** — the full transitive closure of `(trg_tbl, src_tbl)` pairs,
  computed with the repo's iterative BFS "frontier + anti-join" loop (each hop materialized,
  so known pairs are never re-derived and cycles can't loop).
- **`waf_lineage_column_edges`** — deduped column-level edges.

The lineage-heavy checks (`DG-01-03`, `DG-02-01`, `IU-04-03`, `R-02-05`) then read these small
local tables instead of the system tables, so they run significantly faster. (The direct-edge
tables answer the current checks' one-hop questions; the closure is materialized and exposed
via `ctx.lineage_closure_table()` for multi-hop "all upstream sources" analysis.)

Materialization is controlled by `MATERIALIZE_LINEAGE` in `00_config` (default `True`) and runs
only when `PERSIST_RESULTS = True`. If it is skipped or can't be built — no write access to the
catalog/schema, materialization disabled, or a read-only run — each check transparently falls
back to the lineage system tables, so results are identical either way. The tables are rebuilt
(`CREATE OR REPLACE`) on each run.

## Testing

**Offline** — no workspace needed. Covers scoring thresholds, scope SQL generation
(including quote escaping), report rendering, notebook structure, that each pillar's
checks match the published question bank exactly, and that every check behaves on an
empty or locked-down workspace without ever falsely reporting `completed`:

```bash
python -m unittest discover -s tests -v     # 78 tests
python tools/nbutil.py validate             # notebook JSON, syntax, no committed output
```

**Against a live workspace** — executes every check's real SQL through the Databricks
CLI, so query syntax and permissions are verified without starting a cluster. Statuses it
prints reflect the test workspace, not your Databricks environment, so only errors matter:

```bash
python tools/smoke_test.py --profile <PROFILE>                  # all pillars
python tools/smoke_test.py --profile <PROFILE> --pillar cost    # one pillar
```

All 113 checks currently execute cleanly with 0 SQL failures.

## Requirements and permissions

The assessor needs `SELECT` on the system schemas below. `00_config` prints exactly which
are readable before you draw conclusions — a wall of `open` results usually means missing
grants, not missing controls.

| Schema | Used for |
| --- | --- |
| `system.information_schema` | catalog/table/column metadata, constraints, grants, shares |
| `system.access` | audit logs, table and column lineage |
| `system.billing` | cost, serverless and Photon adoption, tagging |
| `system.compute` | cluster and warehouse configuration, utilization |
| `system.query` | query performance, monitoring, client tools |
| `system.lakeflow` | jobs, tasks, run history, pipelines |
| `system.storage` | file sizes, predictive optimization |
| `system.mlflow`, `system.serving` | ML tracking and model serving |

Checks degrade to `open` with an explanatory rationale when a schema is unavailable,
rather than failing the run.

**The assessment is read-only against your Databricks environment's data.** Its only writes
are to the catalog and schema you configure: its own results table and the lineage snapshot
tables (`waf_lineage_edges`, `waf_lineage_column_edges`). With `PERSIST_RESULTS = False` it
writes nothing at all.

## Regenerating the question bank

`waf_questions.py` is generated from the WAF Assessment Tool's published JSON. If the tool
adds or reworks questions, regenerate and the tests will flag any pillar whose checks have
drifted:

```bash
python tools/gen_questions.py
python -m unittest discover -s tests
```

Note: the published bank has no `PE-02-13` — it jumps `PE-02-12` → `PE-02-14`. That gap is
upstream and is preserved rather than renumbered.
