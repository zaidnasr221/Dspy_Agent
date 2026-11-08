"""Isolate one original PDF page and run the existing agent with durable console logs."""
import argparse
from contextlib import redirect_stdout,redirect_stderr
import hashlib
import json
from pathlib import Path
import shutil
import sys
import traceback
import uuid
from facade_dspy.model import add_model_arguments


def main():
    project=Path(__file__).resolve().parent
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf',type=Path,default=project/'data/50SG ALUPCO PDF COMBINED.pdf')
    parser.add_argument('--page',type=int,default=15,help='Physical PDF page number, starting at 1')
    parser.add_argument('--out',type=Path,default=project/'test_results',help='Parent folder for a new, isolated test run')
    parser.add_argument('--index-only',action='store_true',help='Local stages only; no Claude Code or model calls')
    parser.add_argument('--profile',action='store_true',help='Save interactive Pyinstrument reports separately from extraction results')
    parser.add_argument('--budget-usd',type=float)
    parser.add_argument('--accept-cli-cost-estimates',action='store_true')
    add_model_arguments(parser)
    args=parser.parse_args()
    test=args.out.resolve()/f'page{args.page}_{uuid.uuid4().hex}'
    test.mkdir(parents=True)
    summary={'test_folder':str(test),'status':'failed'};exit_code=1
    with (test/'console.log').open('w',encoding='utf-8',buffering=1) as log,redirect_stdout(log),redirect_stderr(log):
        try:
            import pymupdf
            from facade_dspy.program import FacadeExtractionAgent
            from facade_dspy.cli import zip_results
            source=args.pdf.resolve();subset=test/'selected_page.pdf'
            with pymupdf.open(source) as original:
                if original.needs_pass or not 1<=args.page<=len(original):
                    raise ValueError('Source must be unlocked and contain the requested physical PDF page')
                with pymupdf.open() as page:
                    page.insert_pdf(original,from_page=args.page-1,to_page=args.page-1)
                    page.save(subset)
            with source.open('rb') as stream:source_hash=hashlib.file_digest(stream,'sha256').hexdigest()
            provenance=dict(original_pdf=str(source),original_pdf_sha256=source_hash,original_pdf_page=args.page,
                            test_pdf_page=1,scope='Only this physical PDF page; unseen identity/edition evidence remains unknown.')
            (test/'page_selection.json').write_text(json.dumps(provenance,indent=2),encoding='utf-8')
            agent=FacadeExtractionAgent(out=test/'outputs',index_only=args.index_only,pilot_pages=[1],
                                        budget_usd=args.budget_usd,accept_cli_estimates=args.accept_cli_cost_estimates,profile=args.profile,
                                        backend=args.backend,model=args.model,
                                        command=[args.backend_executable] if args.backend_executable else None)
            result=agent(pdf_url=subset.as_uri());run=Path(result.output_directory)
            (run/'original_page_provenance.json').write_text(json.dumps(provenance,indent=2),encoding='utf-8')
            shutil.copy2(test/'outputs/pipeline.log',run/'pipeline.log')
            archive=zip_results(run,test/'results.zip')
            summary.update(output_directory=str(run),coverage=result.coverage,results_zip=str(archive))
            if args.index_only:
                if result.coverage['status']!='indexed_only':raise RuntimeError('Local indexing did not finish')
            else:
                pilot=json.loads((run/'pilot_validation.json').read_text(encoding='utf-8')) if (run/'pilot_validation.json').exists() else {}
                if result.coverage['complete_proposal_pages']!=[1] or not pilot.get('passed'):
                    raise RuntimeError('Full extraction/source verification did not complete; inspect saved coverage and failures')
            summary['status']='indexed_only' if args.index_only else 'complete_proposals_not_engineering_approved'
            exit_code=0
        except Exception as exc:
            traceback.print_exc(file=log)
            summary['error']=type(exc).__name__+': '+str(exc)
        finally:
            if 'agent' in locals() and agent.profiling.report_path:
                summary['profile_report']=str(agent.profiling.report_path)
    (test/'run.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    # Keep Python diagnostics out of native stderr: PowerShell 5.1 may terminate before Tee-Object saves them.
    print(json.dumps(summary,indent=2),flush=True)
    return exit_code


if __name__=='__main__':
    raise SystemExit(main())
