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


def zip_results(run,destination):
    destination=Path(destination).resolve();destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists():raise FileExistsError('Choose a new results ZIP path')
    entries=[p for p in run.rglob('*') if p.is_file() and p.resolve()!=destination and
             p.suffix in ['.pdf','.png','.json','.jsonl','.parquet','.csv','.html','.txt','.log']]
    manifest={str(p.relative_to(run)).replace('\\','/'):filehash(p) for p in entries}
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as z:
        for p in entries:z.write(p,str(p.relative_to(run)))
        z.writestr('FILE_MANIFEST.json',json.dumps(manifest,indent=2))
        z.writestr('READ_FIRST.txt','Open review.html. Data formats are in data/. All records are proposals requiring engineering review. Read coverage.json, cost_summary.json, reviews/ and failed work attempts before use. Raw image/transcription/model responses are evidence, not engineering approval.\n')
    with zipfile.ZipFile(destination) as z:
        if z.testzip():raise ValueError('Results ZIP integrity failure')
    return destination


def main():
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr,'reconfigure'):sys.stderr.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description='DSPy PDF extraction agent using Claude Code as its read-only visual tool')
    parser.add_argument('pdf_url',help='HTTP(S) PDF URL, file:// URL, or local PDF path')
    parser.add_argument('--out',default='runs')
    parser.add_argument('--budget-usd',type=float,help='Explicit cumulative budget for this PDF; no old account balance is assumed')
    parser.add_argument('--reserve-usd',type=float,default=.5,help='Scheduling headroom reserved before each Claude Code invocation')
    parser.add_argument('--cli-budget-usd',type=float,default=.15,help='Claude Code stop threshold; can be exceeded, so reserve headroom')
    parser.add_argument('--accept-cli-cost-estimates',action='store_true',help='Acknowledge CLI estimates are not exact pre-counts or guaranteed billing caps')
    parser.add_argument('--claude-executable',help='Native claude executable path; no shell command string')
    parser.add_argument('--model',default='claude-sonnet-5-5')
    parser.add_argument('--reuse',help='Existing results folder with document.json and full/pages/*.json; source hash must match')
    parser.add_argument('--replay',action='store_true',help='Reuse saved records only; never launch Claude Code')
    parser.add_argument('--index-only',action='store_true',help='Download/render/index locally, with no model calls')
    parser.add_argument('--pilot-pages',help='Comma-separated PDF pages for the initial source-check pilot')
    parser.add_argument('--compare',action='append',default=[],help='Existing other-document results; manufacturer/system matching is mandatory')
    parser.add_argument('--export-zip',help='Create a data-only results ZIP at this new path')
    args=parser.parse_args()
    url=args.pdf_url
    if urlsplit(url).scheme not in ['http','https','file'] and Path(url).is_file():url=Path(url).resolve().as_uri()
    pilot=[int(p) for p in args.pilot_pages.split(',')] if args.pilot_pages else None
    program=FacadeExtractionAgent(out=args.out,budget_usd=args.budget_usd,reserve_usd=args.reserve_usd,
        cli_budget_usd=args.cli_budget_usd,model=args.model,command=[args.claude_executable] if args.claude_executable else None,
        accept_cli_estimates=args.accept_cli_cost_estimates,reuse=args.reuse,replay=args.replay,pilot_pages=pilot,
        compare=args.compare,index_only=args.index_only)
    try:
        with redirect_stdout(sys.stderr):result=program(pdf_url=url)
        output=dict(output_directory=result.output_directory,coverage=result.coverage)
        if args.export_zip:output['results_zip']=str(zip_results(Path(result.output_directory),args.export_zip))
        print(json.dumps(output,ensure_ascii=False,indent=2))
    except Exception as exc:
        parser.exit(2,type(exc).__name__+': '+str(exc)+'\n')


if __name__=='__main__':main()
