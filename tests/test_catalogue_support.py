from facade_dspy.source import article_mentions, support_pages


def test_support_search_matches_printed_article_tokens_and_keeps_original_pages():
    text = 'Cleat 32380 and gasket 237 583'
    assert {'32380', '237583'} <= article_mentions(text)
    pages = [{'pdf_page': 51, 'text': text}, {'pdf_page': 28, 'text': '237583'},
             {'pdf_page': 49, 'text': '32380 237583'}, {'pdf_page': 52, 'text': '12323809'}]
    assert [page['pdf_page'] for page in support_pages(pages, 51, article_mentions(text))] == [49, 28]


def test_scoped_variant_uses_the_occurrence_that_prints_the_size():
    from facade_dspy import articles
    base = {'profile_id': '235918', 'record_id': 'base', 'occurrences': [
        {'label': 'Polyamide nose adaptor', 'callout': '235918 (14.5mm Isolator)', 'provenance': {'pdf_page': 51}},
        {'label': '235918', 'provenance': {'pdf_page': 52}},
    ]}
    variant = {'profile_id': '235918-14.5', 'record_id': 'variant', 'occurrences': [
        {'label': 'Isolator', 'provenance': {'pdf_page': 30}},
    ], 'depth': [{'value': 10, 'unit': 'mm', 'provenance': {'pdf_page': 30}}]}
    index = articles.build_index([base, variant])
    known, unknown = (articles.for_occurrence(base, occurrence, index) for occurrence in base['occurrences'])
    assert articles.linked_facts(known, index, 'depth')[0]['value'] == 10
    assert articles.linked_facts(unknown, index, 'depth') == []


def test_target_page_reads_support_image_and_records_original_scope(tmp_path, monkeypatch):
    from facade_dspy.program import FacadeExtractionAgent
    from facade_dspy.storage import read
    from test_agent import pdf_file
    from test_relationship_pipeline import PipelineBackend

    class Backend(PipelineBackend):
        def execute(self, task, prompt, schema, folder, heartbeat):
            if task == 'extraction' and 'p0001:' in prompt:
                assert (folder / 'support_p0002.png').is_file()
                assert read(folder / 'support_pages.json')[0]['pdf_page'] == 2
            return super().execute(task, prompt, schema, folder, heartbeat)

    monkeypatch.setattr('facade_dspy.program.catalogue_support',
                        lambda *_: {'1': [{'pdf_page': 2, 'articles': ['PP100'], 'reason': 'article_match'}]})
    pdf = pdf_file(tmp_path / 'source.pdf', pages=2)
    calls = []
    agent = FacadeExtractionAgent(out=tmp_path / 'runs', budget_usd=1, target_pages=[1],
                                  backend_factory=lambda *_: Backend(calls))
    result = agent(pdf_url=pdf.as_uri())
    assert result.coverage['target_pages'] == [1]
    assert result.coverage['remaining_target_pages'] == []
    assert result.coverage['complete_proposal_pages'] == [1, 2]


def test_repeated_base_id_table_rows_keep_the_target_variant_size():
    from facade_dspy import articles
    def row(size, y):
        return dict(provenance={'pdf_page': 62, 'bbox': [0.1, y, 0.9, y + .02]},
                    fields=[{'header': 'Shape annotation', 'value': f'{size}mm', 'unit': None}])

    occurrence = {'label': 'Nose adaptor', 'callout': '235918 (14.5mm Isolator)',
                  'provenance': {'pdf_page': 51}}
    record = {'profile_id': '235918', 'record_id': 'base', 'occurrences': [occurrence],
              'table_observations': [row(18.5, .3), row(14.5, .4)]}
    index = articles.build_index([record])
    resolved = articles.for_occurrence(record, occurrence, index)
    assert [fact['value'] for fact in articles.linked_facts(resolved, index, 'shape_annotation')] == ['14.5mm']
    unresolved = articles.for_occurrence(record, dict(occurrence, callout='235918'), index)
    assert articles.linked_facts(unresolved, index, 'shape_annotation') == []


def test_conventional_glazing_heading_needs_verified_fixing_and_visible_glass():
    from facade_dspy.assembly import conventional_glazing
    detail = dict(system_type='CC', provenance={'pdf_page': 51})
    nodes = [{'id': 'glass', 'kind': 'glass'}]
    fix = dict(graph_eligible=True, relation='fixed', fastener_assigned={'type': 'screw'},
               source_component={'kind': 'pressure plate'}, target_component={'kind': 'transom'},
               evidence='Screw through pressure plate into transom')
    text = [dict(text='Conventional Glazing', provenance={'pdf_page': 51})]
    assert conventional_glazing(detail, nodes, [fix], text)['type'] == 'CC'
    assert conventional_glazing(detail, [], [fix], text) is None
    assert conventional_glazing(detail, nodes, [dict(fix, graph_eligible=False)], text) is None
    assert conventional_glazing(detail, nodes, [fix], []) is None


def test_number_header_identifies_article_only_in_parts_table():
    from facade_dspy.articles import article_column
    assert article_column('Number', ['Shape annotation', 'Description', 'Number', 'Material'])
    assert not article_column('Number', ['Number', 'Drawing', 'Scale'])
