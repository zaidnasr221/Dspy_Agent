"""One PDF URL in; source-scoped review/data artifacts out."""
import argparse
from pathlib import Path
import json
import zipfile
from urllib.parse import urlsplit
from contextlib import redirect_stdout
import sys
from .program import FacadeExtractionAgent
from .storage import filehash
from .model import add_model_arguments
from .publication import published_directory


def zip_results(run,destination,snapshot=None):
    destination=Path(destination).resolve();destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists():raise FileExistsError('Choose a new results ZIP path')
    published=Path(snapshot).resolve() if snapshot is not None else published_directory(run)
    entries=[p for p in published.rglob('*') if p.is_file() and p.resolve()!=destination and
             p.suffix in ['.pdf','.png','.json','.jsonl','.parquet','.csv','.html','.txt','.log']]
    files={p.relative_to(published).as_posix():p for p in entries}
    if published!=run.resolve():
        for name in ('pipeline.log','original_page_provenance.json'):
            if (run/name).is_file():files[name]=run/name
    manifest={name:filehash(p) for name,p in files.items()}
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as z:
        for name,p in files.items():z.write(p,name)
        z.writestr('FILE_MANIFEST.json',json.dumps(manifest,indent=2))
        z.writestr('READ_FIRST.txt','Open review.html. Data formats are in data/. All records are proposals requiring engineering review. Read coverage.json, cost_summary.json, reviews/ and failed work attempts before use. Raw image/transcription/model responses are evidence, not engineering approval.\n')
    with zipfile.ZipFile(destination) as z:
        if z.testzip():raise ValueError('Results ZIP integrity failure')
    return destination


def page_numbers(value):
    """Parse physical page numbers and inclusive ranges in requested order."""
    pages = {}
    try:
        for item in value.split(','):
            first, separator, last = item.strip().partition('-')
            start = int(first)
            stop = int(last) if separator else start
            if not 1 <= start <= stop <= 2000:
                raise ValueError
            pages.update(dict.fromkeys(range(start, stop + 1)))
    except ValueError as error:
        raise argparse.ArgumentTypeError('Use page numbers or ranges such as 10-19,51') from error
    return list(pages)


def build_parser():
    parser=argparse.ArgumentParser(description='DSPy PDF extraction agent using Claude Code or OpenCode/OpenAI as its visual tool')
    parser.add_argument('pdf_url',help='HTTP(S) PDF URL, file:// URL, or local PDF path')
    parser.add_argument('--out',default='runs')
    parser.add_argument('--budget-usd',type=float,help='Explicit cumulative budget for this PDF; no old account balance is assumed')
    parser.add_argument('--reserve-usd',type=float,default=.5,help='Scheduling headroom reserved before each backend invocation')
    parser.add_argument('--cli-budget-usd',type=float,default=.15,help='Claude Code stop threshold; can be exceeded, so reserve headroom')
    parser.add_argument('--accept-cli-cost-estimates',action='store_true',help='Acknowledge CLI estimates are not exact pre-counts or guaranteed billing caps')
    parser.add_argument('--claude-executable',help='Native claude executable path; no shell command string')
    add_model_arguments(parser)
    parser.add_argument('--reuse',help='Existing results folder with document.json and full/pages/*.json; source hash must match')
    parser.add_argument('--replay',action='store_true',help='Reuse saved records only; never launch either model backend')
    parser.add_argument('--index-only',action='store_true',help='Download/render/index locally, with no model calls')
    parser.add_argument('--profile',action='store_true',help='Save Pyinstrument HTML and stage timings in a new out/profiles/ folder')
    parser.add_argument('--pilot-pages',type=page_numbers,help='Physical pages for the initial source check, e.g. 10-11')
    parser.add_argument('--target-pages',type=page_numbers,help='Physical page numbers or ranges, e.g. 10-19,51')
    parser.add_argument('--mlflow-db',type=Path,help='Optional local MLflow SQLite database for DSPy and pipeline traces')
    parser.add_argument('--compare',action='append',default=[],help='Existing other-document results; manufacturer/system matching is mandatory')
    parser.add_argument('--export-zip',help='Create a data-only results ZIP at this new path')
    return parser


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.claude_executable and args.backend_executable:parser.error('Use only one executable override')
    if args.claude_executable and args.backend=='opencode':parser.error('--claude-executable is only for Claude Code; use --backend-executable')
    url=args.pdf_url
    if urlsplit(url).scheme not in ['http','https','file'] and Path(url).is_file():url=Path(url).resolve().as_uri()
    if args.mlflow_db:
        import mlflow
        database=args.mlflow_db.resolve()
        database.parent.mkdir(parents=True,exist_ok=True)
        mlflow.set_tracking_uri('sqlite:///'+database.as_posix())
        mlflow.set_experiment('facade-catalogue')
        mlflow.dspy.autolog()
    program=FacadeExtractionAgent(out=args.out,budget_usd=args.budget_usd,reserve_usd=args.reserve_usd,
        cli_budget_usd=args.cli_budget_usd,model=args.model,backend=args.backend or ('claude-code' if args.claude_executable else None),
        command=[args.backend_executable or args.claude_executable] if args.backend_executable or args.claude_executable else None,
        accept_cli_estimates=args.accept_cli_cost_estimates,reuse=args.reuse,replay=args.replay,pilot_pages=args.pilot_pages,
        compare=args.compare,index_only=args.index_only,profile=args.profile,target_pages=args.target_pages,trace=bool(args.mlflow_db))
    try:
        with redirect_stdout(sys.stderr):result=program(pdf_url=url)
        output=dict(output_directory=result.output_directory,snapshot_directory=result.snapshot_directory,coverage=result.coverage)
        if program.profiling.report_path:output['profile_report']=str(program.profiling.report_path)
        if args.export_zip:output['results_zip']=str(zip_results(Path(result.output_directory),args.export_zip,result.snapshot_directory))
        if args.mlflow_db:
            output['mlflow_trace_id']=mlflow.get_last_active_trace_id()
            mlflow.flush_trace_async_logging()
        print(json.dumps(output,ensure_ascii=False,indent=2))
    except Exception as exc:
        parser.exit(2,type(exc).__name__+': '+str(exc)+'\n')


if __name__=='__main__':main()
