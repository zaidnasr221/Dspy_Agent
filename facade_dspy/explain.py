"""Evidence-based explanations and a labelled audit of saved catalogue results."""
import argparse
from collections import Counter
from pathlib import Path

from .publication import published_directory
from .storage import dump, read


FACTS = ('depth', 'edge_spacing', 'spacing', 'quantity', 'material', 'finish', 'color')


def page_explanation(snapshot, page):
    snapshot = Path(snapshot)
    details = []
    article_ids = set()
    for detail in read(snapshot/'data/detail_outputs.json'):
        if detail['page'] != page:
            continue
        articles = []
        for article in detail['article_database']:
            if article.get('profile_id'):
                article_ids.add(article['profile_id'])
            articles.append(dict(
                article_id=article.get('profile_id'), occurrence_id=article['occurrence_id'],
                label=article['label'], facts={name:article[name] for name in FACTS if article.get(name) is not None},
                other=article.get('other', {}), conflicts=article.get('conflicts', []),
                missing_information=article.get('missing_information', [])))
        details.append(dict(detail_id=detail['detail_id'], title=detail['title'], system=detail['system'],
                            articles=articles, assemblies=detail['assembly_database'], unresolved=detail['unresolved']))
    candidates = {record['profile_id']:record.get('table_observations', [])
                  for record in read(snapshot/'data/article_database.json') if record.get('profile_id') in article_ids}
    claims = [dict(claim_id=row['claim_id'], predicate=row['predicate'], source=row['source'], target=row['target'],
                   status=row['relationship_status'], reasons=row['status_reasons'],
                   evidence=row['evidence'], provenance=row['provenance'])
              for row in read(snapshot/'data/relationships.json') if row['provenance']['pdf_page']==page]
    return dict(page=page, details=details, catalogue_candidates=candidates, relationship_decisions=claims)


def audit(run, database):
    """Record a new evidence audit, keeping the original execution traces intact."""
    import mlflow
    from mlflow.tracing.attachments import Attachment

    run = Path(run).resolve()
    snapshot = published_directory(run)
    coverage = read(snapshot/'coverage.json')
    targets = coverage.get('target_pages', coverage['complete_proposal_pages'])
    mlflow.set_tracking_uri('sqlite:///'+Path(database).resolve().as_posix())
    mlflow.set_experiment('facade-catalogue')
    report = {}
    with mlflow.start_span(name='Evidence audit from saved results', span_type='CHAIN') as root:
        root.set_inputs(dict(snapshot=str(snapshot), target_pages=targets,
                             origin='Saved exports and model receipts', new_model_calls=0))
        mlflow.update_current_trace(tags={'audit_type':'saved_evidence','new_model_calls':'0'})
        for page in targets:
            explanation = page_explanation(snapshot, page)
            supports = coverage.get('support_pages', {}).get(str(page), [])
            with mlflow.start_span(name=f'Page {page} evidence and decisions', span_type='CHAIN') as span:
                images = {f'PDF page {n}':Attachment.from_file(snapshot/'images'/f'p{n:04}.png')
                          for n in dict.fromkeys([page, *(item['pdf_page'] for item in supports)])}
                span.set_inputs(dict(original_pdf_page=page, selected_support=supports, source_images=images))
                span.set_outputs(explanation)
                span.set_attributes({'page':page,'detail_count':len(explanation['details'])})
            report[str(page)] = explanation
        root.set_outputs(dict(pages=targets,
                              relationship_statuses=dict(Counter(row['status'] for item in report.values()
                                                                  for row in item['relationship_decisions']))))
        trace_id = root.trace_id
    dump(run/'data/explanations.json', dict(origin='Saved evidence audit', pages=report, mlflow_trace_id=trace_id))
    mlflow.flush_trace_async_logging()
    persisted = mlflow.get_trace(trace_id)
    if persisted is None or len(persisted.data.spans) != len(targets)+1:
        raise RuntimeError('Evidence audit was not persisted completely')
    return dict(trace_id=trace_id, pages=len(targets), spans=len(persisted.data.spans))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=Path)
    parser.add_argument('--mlflow-db',required=True,type=Path)
    args=parser.parse_args()
    print(audit(args.run,args.mlflow_db))
