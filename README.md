# Facade extraction with DSPy and Claude Code

**Input:** a PDF URL. **Process:** index → read images → validate → check a pilot → extract remaining regions → reconcile. **Output:** source-linked HTML, JSON, Parquet, CSV and an optional data ZIP.

`FacadeExtractionAgent` is a real `dspy.Module` with three `dspy.Tool` calls: inspect document identity, read a region, and check a proposed region against its source. Local code selects jobs and enforces checks. It does not need another model to plan the workflow. No DSPy optimizer or training run is included.

## Run

On this workspace, Python dependencies and Claude Code 2.1.318 are installed locally.

Edit `.env` beside this README (`E:\projects_DomApp\details_ocr_graph\facade_dspy\.env`) and enter your key locally:

```dotenv
ANTHROPIC_API_KEY=your_key_here
```

`FacadeExtractionAgent.__init__` loads this exact project file with `python-dotenv`, including when called from another working directory or Python code. DSPy itself does not automatically load `.env`. Existing process environment values take precedence (`override=False`). The Claude Code adapter checks `ANTHROPIC_API_KEY` and passes the environment to its subprocess; no key is embedded in prompts or code. Bare-mode Claude Code uses this key instead of subscription login. An empty key is sufficient for local indexing/replay, but live model work requires a configured key. Keep `.env` private; Git ignores it. `.env.example` is a shareable blank template. [Claude Code documentation](https://code.claude.com/docs/en/headless#start-faster-with-bare-mode)

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

Every Claude Code request includes exact input file paths and required image Read calls. The restricted Read tool cannot list a directory. Responses without successful source-image reads are rejected even when the CLI reports success. Inspect `raw_responses/*.read_validation.json` for missing image reads and tool errors; the original reply and reported usage remain preserved. Failed attempts are not retried automatically.

Provider errors retain their actual HTTP status and reason, such as `401 Invalid API key`, rather than being reported as incomplete image reads. A key being present in `.env` does not establish that it is valid; an inherited `ANTHROPIC_API_KEY` also takes precedence over `.env`. Use `--index-only` for local processing without API calls. Existing failed-run evidence is preserved.

For a new machine:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
npm install --prefix .local_setup/claude --no-audit --no-fund @anthropic-ai/claude-code@2.1.318
```

A native `claude` on PATH also works. Use `--claude-executable` to select another native executable. Python 3.11+ is required; npm installation needs Node 22+. Existing extraction folders and the three PDF ZIPs remain untouched.

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

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests use synthetic PDFs, archived page data, a local HTTP fixture and a fake CLI. They test the actual DSPy module/tool calls and subprocess protocol without using paid models. The real Claude Code binary was checked with `--version` / `--help`; authenticated live OCR is intentionally not exercised here.
