# Facade extraction with DSPy, Claude Code and OpenCode/OpenAI

**Input:** a PDF URL. **Process:** index → read images → validate → check a pilot → extract remaining regions → reconcile. **Output:** source-linked HTML, JSON, Parquet, CSV and an optional data ZIP.

`FacadeExtractionAgent` is a real `dspy.Module` with three `dspy.Tool` calls: inspect document identity, read a region, and check a proposed region against its source. Local code selects jobs and enforces checks. It does not need another model to plan the workflow. No DSPy optimizer or training run is included.

## Quick start with OpenCode and OpenAI

OpenCode is the default backend and uses `openai/gpt-6.1-sol`. On a new Windows
checkout, use Python 3.11+ and Node.js with npm, then run the following from
the project directory.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
npm.cmd install --prefix .local_setup/opencode --no-audit --no-fund opencode-ai@1.19.0
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
notepad.exe .env
```

Set `OPENAI_API_KEY` in the project `.env` and save it locally.

```dotenv
OPENAI_API_KEY=your_openai_key_here
```

`FacadeExtractionAgent` loads the project `.env` using `python-dotenv` even
when called from a different working directory. Existing process environment
values take precedence. If a stale shell value is taking priority, clear it
with `Remove-Item Env:\OPENAI_API_KEY -ErrorAction SilentlyContinue`.
OpenCode receives the key through its environment and uses the same model for
document identity, region extraction and source review. Git ignores `.env`.

Missing or blank credentials and keys containing whitespace fail before
OpenCode launches. An authentication rejection from OpenAI during a live run
surfaces as an `OPENAI_API_KEY` error, with redacted diagnostics in the run
folder. After correcting a rejected key, use a fresh output directory.

Run the full pipeline with a budget you authorize. This example uses the local
ALUPCO PDF. Set `$Pdf` to another local PDF path or a direct PDF URL as needed.

```powershell
$Pdf = (Resolve-Path -LiteralPath '.\data\50SG ALUPCO PDF COMBINED.pdf').Path
$Out = Join-Path (Get-Location).Path ('runs\opencode_' + [guid]::NewGuid().ToString('N'))
$Budget = Read-Host 'Authorized cumulative budget in USD, using a decimal point'
if ($Budget -notmatch '^[0-9]+(\.[0-9]+)?$' -or [double]$Budget -lt 0.50) {
    throw 'Enter a USD budget of at least 0.50.'
}
.\.venv\Scripts\python.exe -u -B -m facade_dspy $Pdf --out $Out --budget-usd $Budget `
    --accept-cli-cost-estimates --export-zip (Join-Path $Out 'results.zip')
if ($LASTEXITCODE -ne 0) { throw "Extraction failed. See $Out\pipeline.log." }
```

Check `coverage.status` and `remaining_pages` in the printed JSON. Open
`review.html` inside `snapshot_directory`. A budget stop or pilot failure
can produce a partial snapshot. OpenCode's budget is a scheduling allowance,
and the CLI lacks a per-call dollar cap.

If OpenAI reports insufficient credits, add API credits to the organization
associated with your key. For a rate limit, wait for the limit to reset. Rerun
with the same PDF, `--out`, backend, and model. The queue resumes the failed
job and reuses completed checkpoints. OpenCode uses a temporary loopback
connection to prevent billing rejections from triggering upstream retries.

## Backend and model configuration

`facade_dspy/model.py` is the single source of truth for supported backends,
default models, key-variable names, argument definitions and adapter creation.
Both entry points and programmatic callers use it. No direct OpenAI SDK calls or
extra DSPy planner model were added.

| Backend | Default model | Required variable |
|---|---|---|
| `opencode` (default) | `openai/gpt-6.1-sol` | `OPENAI_API_KEY` |
| `claude-code` | `claude-sonnet-5-5` | `ANTHROPIC_API_KEY` |

Selection precedence: explicit `--backend` / `--model` (or Python arguments),
then `FACADE_BACKEND` / `FACADE_MODEL` from the environment and project `.env`,
then defaults in `model.py`. Process environment values take precedence over
`.env`. Only the selected backend's key is required for live work.

Optional `.env` selection, with no key values shown:

```dotenv
FACADE_BACKEND=opencode
FACADE_MODEL=openai/gpt-6.1-sol
```

Both the regular CLI and `test_pdf_page.py` accept `--backend`, `--model` and
`--backend-executable`. Existing `--claude-executable` on the regular CLI remains
supported for Claude Code. A different model must support images and tool calls;
the OpenCode adapter accepts only `openai/<model>` identifiers. Model/account
availability is not checked by making an API request.

Example switches to add to a **future authorized live command**:

```powershell
# Claude Code:
--backend claude-code --model claude-sonnet-5-5
# OpenCode using the OpenAI API:
--backend opencode --model openai/gpt-6.1-sol
```

For an offline page test, either selection can be added alongside `--index-only`.
Index-only and replay never instantiate either live backend and do not need keys.
They do not establish that AI vision extraction works with an operator's account.

The existing `backend_factory(run, budget)` contract is retained. The default
factory in `model.py` creates the selected adapter; custom factories still work.
Both adapters feed the same identity, region-reading and source-review tools,
Pydantic schemas, structural checks, normalization, knowledge graph assembly,
Polars exports and review HTML. The original Python `agent.claude_code` attribute
remains an alias of the provider-neutral `agent.read_region` tool. OpenCode results
record `OpenCode/OpenAI visual reading` in provenance rather than claiming Claude
read them.

`model_selection.json` binds each live run to its requested backend/model. A
different selection requires a fresh output folder; no automatic fallback or
cross-backend overwrite is performed. Adapter request hashes include the model
and CLI version; OpenCode also has its own backend-tagged cache namespace. As
with earlier producer changes, preserve historical checkpoints and start fresh
rather than silently resuming old extraction jobs.

### OpenCode transport and offline verification

The adapter uses OpenCode `>=1.19,<2` and its `run --pure --format json` event
protocol. Your installed 1.19.0 binary was checked using `--version`, `run --help`
and isolated `debug config` with a nonfunctional test credential; no model prompt
was submitted. Native executable discovery handles the npm Windows shim without
executing `.cmd`, `.bat` or `.ps1` scripts through a shell. On a new machine,
install `opencode-ai@1.19.0`, or supply its native path through
`--backend-executable`.

Each invocation gets isolated OpenCode configuration/data/cache/state directories
under `opencode_runtime/`. Runtime configuration enables only OpenAI, references
`{env:OPENAI_API_KEY}`, selects the official OpenAI API endpoint, disables sharing,
external plugins, title generation and automatic compaction, and denies tools
except reads of the exact registered input files. Project instructions and
external skill discovery are disabled. Source documents remain untrusted data.
No automatic provider fallback is configured.

The adapter checks completed `read` events against exact source paths, requires
a complete terminal step and parses/validates the final JSON against the supplied
schema before the shared pipeline checks. Plain JSON-looking text or reading a
different file with the same basename does not satisfy image evidence. Failed
reads, incomplete replies, errors, stderr and usage remain in `raw_responses/`;
they are not cached as successful results. Diagnostic secrets are redacted.

OpenCode has **no CLI per-call dollar limit** equivalent to Claude Code's
`--max-budget-usd`; `--cli-budget-usd` applies only to Claude Code. Both use the
existing cumulative scheduling reservations and explicit cost-estimate acceptance.
OpenCode step costs are deduplicated; incomplete usage or an unexplained zero cost
for a paid generation keeps the full reservation. Token-limit failures can still
settle known billed usage. Neither backend guarantees a hard billing ceiling;
CLI-internal retries are outside the adapter's control. Exact full-conversation
token counts are unavailable before generation. No model work was run as part
of this integration.

Offline tests use synthetic PDFs and fake CLI processes for both backends. They
exercise shared vision-stage contracts, pilot review, knowledge graph/data
outputs, selection guards, caching, missing/wrong source reads, malformed JSON,
provider errors and usage accounting without external model calls. This does
not establish live OCR quality or authenticated account compatibility.

Protocol references: [OpenCode CLI](https://opencode.ai/docs/cli/),
[configuration](https://opencode.ai/docs/config/),
[OpenCode 1.19.0 image Read implementation](https://github.com/anomalyco/opencode/blob/v1.19.0/packages/opencode/src/tool/read.ts),
[OpenAI model catalogue](https://developers.openai.com/api/docs/models).

## Existing commands

Run from the project folder:

```powershell
.\run.ps1 "https://example.com/catalogue.pdf" --budget-usd 10 --accept-cli-cost-estimates --export-zip results.zip
```

Replace the example URL with a direct PDF URL. The budget is an explicit cumulative allowance for that PDF workspace, not an account balance. This implementation task made **no paid model calls**.

For physical PDF page 15 of the local ALUPCO catalogue only:

```powershell
.\.venv\Scripts\python.exe -u -B .\test_pdf_page.py --page 15 --budget-usd 10 --accept-cli-cost-estimates
```

Add `--index-only` for a local test without model calls. This runner creates a fresh `test_results/page15_<unique ID>/`, isolates original page 15 as test PDF page 1, and saves the page mapping, data, review HTML and results ZIP. `console.log` captures preparation errors; `outputs/pipeline.log` captures stage progress, third-party notices and full Python tracebacks. The agent also uses this log when called directly. The runner reports failure with a nonzero exit code and the test folder path. Existing outputs are never reused or overwritten. Windows PowerShell 5.1 must not receive informational native stderr through `2>&1` with `ErrorActionPreference=Stop`; diagnostics are captured inside Python instead.

For Claude Code, set `ANTHROPIC_API_KEY` in `.env` and select
`--backend claude-code --model claude-sonnet-5-5` with a fresh output
directory. Install its CLI locally if needed:

```powershell
npm.cmd install --prefix .local_setup/claude --no-audit --no-fund @anthropic-ai/claude-code@2.1.318
```

A native `claude` on PATH also works. Use `--claude-executable` to select
another executable. Both adapters require successful source image reads
and retain failure details in `raw_responses/`.

## Use existing results without a model

```powershell
.\run.ps1 "C:\path\catalogue.pdf" --reuse "C:\path\unpacked-results" --replay --out replay_runs
```

The source PDF hash must match `document.json`. The importer accepts the old `full/pages/p*.json` format and our independent ZIPs' `outputs/` layout. It prefers `reviewed_pages/`. Partial-region files cannot be imported as completed pages. Completed exports from this program are also reusable.

`--index-only` downloads and builds native text, references, images and candidate regions locally, without Claude Code. `--compare C:\other-results` adds manufacturer/system-scoped cross-document comparisons. Different manufacturers/systems stay separate; unknown identity prevents linking.

## What the program preserves

- Original PDF, content hash, identity/edition evidence and cover/copyright differences.
- Native words and coordinates as search evidence; **Claude Code reads source images for OCR/interpretation**.
- Small region jobs, full-page/header context, raw replies, transcription, repairs, failures, checkpoints and usage receipts.
- Existing queue transactions, dependencies, attempts and leases, with portable `run:` / `code:` paths and active heartbeats.
- A source-check pilot before scaling. Dense incomplete responses are retained and split; failed source checks block scaling. Unknown generation costs remain reserved.
- Separate component definitions, drawing instances, connections, machining features, tools and consumables.
- Literal article IDs, table headers/units/rows/footnotes, alternatives, conditions and explicit unknowns. Drawing references/callouts are not components; holes do not establish screw quantities.
- Same-manufacturer/system article links, with both editions and conflicting claims retained. Compatibility stays unknown without supporting evidence.

All active descriptions/reports are English. Verbatim native/OCR evidence can retain the source language. Model checks, structural checks and human engineering approval are different states; **all extracted knowledge remains proposed**.

## Output files

Analytical exports include typed `canonical_id` keys and `canonical_*` foreign
keys for graph joins. Source IDs and legacy columns are retained. Entity and
detail keys are scoped to the document and region, while component keys are
scoped to the manufacturer/system or to the document if identity is unresolved.

Relationship exports keep the original predicate, its canonical predicate, the
validation status, and any reasons. Use `data/graph_relationships.*` for eligible
engineering proposals and `data/relationships.*` to review every extracted
edge. Graph eligibility does not imply human approval.

A full extraction or replay pins the effective manufacturer profile, its version
and hashes in `manufacturer_profile.json`. Configuration edits take effect in
new workspaces. An existing workspace and a replay of its snapshot use the saved
profile. Known legacy checkpoints keep their original commits and use a recorded
built-in profile baseline when historical configuration was unavailable.
Unexpected checkpoint or artifact changes stop resume without rewriting commits.

Replay from a published run or snapshot preserves committed region proposals and
source reviews, including partial and disputed regions. New snapshots include
`replay_manifest.json` to detect changes to that evidence. Legacy page-only
bundles can still be imported. Their edges remain ineligible when the original
review evidence is unavailable.
Measurements resolve shared local IDs as entity first, matching the legacy
page merger. Canonical keys identify records within their source scope.

Each successful export publishes a complete generation under `snapshots/`
and atomically advances `publication.json`. Use the printed
`snapshot_directory` for a stable view. Only queue-committed proposals and
reviews enter a generation. Replay, comparison and ZIP export pin that
generation, with support for historical bundles. Root-level report and data
paths are compatibility copies. Failed and unpublished evidence remains in
the run workspace for diagnosis. Snapshots are retained for audit.

| Location | Contents |
|---|---|
| `runs/<PDF hash>/review.html` | Searchable review with source drawings/table crops |
| `data/*.json` | Components, entities, relationships, measurements, tables, instructions, questions, conflicts and comparisons |
| `data/*.parquet` | Polars analytical exports; nested evidence is retained as JSON columns |
| `data/*.csv` | Flat exports; `null` represents unknown values |
| `source.pdf`, `images/`, `crops/`, `index/` | Source and reference evidence |
| `raw_responses/`, `tool_cache/`, `proposals/`, `reviews/` | Original model receipts, cached results and proposed readings |
| `coverage.json`, `cost_summary.json`, `cost_ledger.json` | Progress, remaining work and cumulative usage/reservations |
| `work/` | Durable SQLite queue, attempts, events and checkpoint hashes |
| `full/pages/` | Compatibility export created only when all leaf regions of a page are complete |

`schemas/` documents the structured model contract. `examples/EM070_existing_proposal.json` and its image reuse the old MX result; they were not re-extracted. `REUSED_CODE.json` records the code we adapted.

## Cost and review limits

Claude Code does not expose an exact pre-generation token count for its full internal conversation. The program records local input sizes, reserves scheduling headroom, sets a per-invocation CLI threshold, and stores returned token/cost usage. The CLI threshold can be exceeded and its estimates can differ from billing. Therefore live runs require `--accept-cli-cost-estimates`; **this is not the old direct-API hard reservation guarantee**. [CLI budget semantics](https://code.claude.com/docs/en/cli-reference#cli-flags)

Region planning starts with two non-overlapping bands per page, retaining full-page context. Table/drawing boundaries are candidates until visually interpreted. Selected model checks do not verify every measurement, material, matrix cell or engineering relationship. A completed region/page means a validated proposal, not engineering approval.

Resume by running the same command and output directory. Completed checkpoints and exact tool requests are reused. Budget refusals can resume under an explicitly increased allowance. Interrupted/invalid generations are not blindly retried. Preserve a run before changing its inputs or producer code; code changes require inspection/new-workspace reuse rather than silently re-extracting historical results.

## Performance profiling (opt-in)

Install the optional profiler in the existing virtual environment:

```powershell
Set-Location 'E:\projects_DomApp\details_ocr_graph\facade_dspy'
.\.venv\Scripts\python.exe -m pip install --use-feature=truststore ".[profile]"
```

Add `--profile` to either the regular CLI or `test_pdf_page.py`. Without this flag,
no sampling starts and no profile files are created. Programmatic callers can use
`FacadeExtractionAgent(..., profile=True)`.

Offline profiling of ALUPCO physical page 15 only:

```powershell
.\.venv\Scripts\python.exe -u -B .\test_pdf_page.py `
    --pdf '.\data\50SG ALUPCO PDF COMBINED.pdf' `
    --page 15 --index-only --profile
```

For a **full run that calls Claude**, use the same command with
`--page 15 --profile --budget-usd 1.00 --accept-cli-cost-estimates` instead of
`--page 15 --index-only --profile`. It uses the existing `.env`; profiling does not
change credentials, budgets, caching, source review or extraction behavior.
The existing CLI cost limitations still apply.

The printed `profile_report` points to `outputs/profiles/<unique ID>/index.html`
inside a new test folder. Regular CLI runs use `<--out>/profiles/<unique ID>/`.
Each invocation gets a separate profile folder, including failed runs. Profiling
files are kept outside the PDF's hashed results directory and its data-only ZIP.
Existing results are not overwritten. Since this instrumentation changes producer
code, use a fresh output folder rather than resuming historical checkpoints.

Reports include sortable stage timings, filterable slow functions, an interactive
whole-pipeline Pyinstrument call tree, a selectable call tree for each measured
stage invocation, `summary.json`, `pipeline.json` and `pipeline.pyisession`.
Use `python -m pyinstrument --load '<path>\pipeline.pyisession' -r html -o '<new path>.html'`
to render a saved session again without running extraction.

Measured stages cover acquisition, indexing, checkpoint audit, document identity,
region planning, saved-result import, extraction, image preparation, response schema
validation, normalization, structural validation, source review, reconciliation /
knowledge-graph assembly / exports, and queue snapshotting. The last processing
stage stays combined because it is one function in the existing architecture;
its call tree exposes the slower nested functions without restructuring it.

Times are **elapsed wall time**, including disk I/O and Claude Code subprocess
waiting. The profiler cannot inspect subprocess internals or remote Claude/OCR
execution. Inclusive nested stage times overlap; use exclusive stage totals for
comparisons. Very short functions may have no samples, although stage timers
still record them. Profiling adds overhead. Agent initialization, test-page
isolation, final ZIP creation and report rendering are outside the measured agent
execution. Unreached stages are explicitly listed rather than reported as zero.
Offline/index-only reports provide no measurements of live model performance.
No optimizations are applied by this feature.

Profiling/report-writing errors are logged to `pipeline.log` and do not replace
an existing pipeline result or exception. Requesting profiling without the
optional dependency fails early with an installation instruction.

Implementation follows the [Pyinstrument Python API](https://pyinstrument.readthedocs.io/en/latest/reference.html).

## Offline accuracy benchmark

Evaluate saved proposals against independently reviewed ground truth. Each case
labels one complete region. Copy its source hash, physical PDF page, unit ID and
normalized bounds from the evidence, then label the expected records using the
existing `PageResult` schema. Keep the ground truth separate from the predictions.

This example shows the manifest format for an intentionally empty region.
Replace the placeholder hash and reviewer with the actual values.

```json
{
  "version": 1,
  "cases": [
    {
      "id": "catalogue-page-1-top",
      "reviewed_by": "Reviewer name",
      "source_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
      "unit_id": "p0001:top",
      "bbox": [0, 0, 1, 0.5],
      "expected": {
        "pdf_page": 1, "page_kind": "detail", "details": [], "nodes": [],
        "relations": [], "measurements": [], "tables": [], "instructions": [],
        "unresolved": []
      }
    }
  ]
}
```

```powershell
.\.venv\Scripts\python.exe -B -m facade_dspy.benchmark `
    --ground-truth '.\ground_truth.json' --predictions '.\path\to\run'
```

`--predictions` also accepts a snapshot directory or a saved
`data/all_proposals.json` file. Repeat it for multiple catalogues. The scorer
reads local JSON and prints a JSON report. It matches cases by PDF hash, page,
unit ID and bounds. Missing predictions contribute misses. Unlabeled regions
are counted separately. Duplicate labels or predictions are rejected.

The report gives precision, recall, F1 and the underlying counts by case and
pooled by metric. It scores literal article observations, dimension values,
known measurement units, table headers and units, cell contents in position,
and directed relationships with their conditions and evidence basis. Matches
are exact, with multiplicity preserved. Model-local node IDs may differ.
Article and callout identify linked occurrences for scoring, so repeated
occurrences with the same pair are indistinguishable. The scorer applies no
numeric tolerance or unit conversion. Metrics with no observations on either
side have a null F1. Synthetic regression fixtures establish scoring behavior.
Real catalogue accuracy requires separately reviewed labels.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests use synthetic PDFs, archived page data, a local HTTP fixture and a fake CLI. They test the actual DSPy module/tool calls and subprocess protocol without using paid models. The real Claude Code binary was checked with `--version` / `--help`; authenticated live OCR is intentionally not exercised here.
