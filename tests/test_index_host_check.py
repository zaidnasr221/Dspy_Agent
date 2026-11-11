from facade_dspy.index_host_check import build_article_index, questions_for_page


def page(number, articles, edge=None):
    return dict(page=number, observations=dict(
        parts=[dict(id=article, article_id=article, label=label, location='drawing')
               for article, label in articles],
        interfaces=[edge] if edge else []))


def test_support_requires_cleat_and_alternative_hosts_in_the_same_page():
    parts = [('17804', 'cleat'), ('TC5010', 'vent sash'), ('TC5020', 'vent frame')]
    target = page(19, parts, dict(source='17804', target='TC5020', relation='insertion'))
    nearby = page(18, parts[:2])
    corroborating = page(20, parts)
    index = build_article_index([target, nearby, corroborating])

    question, = questions_for_page(target, index)
    assert question['support'] == 20
    assert question['original_host'] == 'TC5020'
    assert {part['article_id'] for part in question['candidates']} == {'TC5010', 'TC5020'}
