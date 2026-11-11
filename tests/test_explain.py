
from pathlib import Path

from facade_dspy.explain import page_explanation
from facade_dspy.storage import dump


def test_explanation_preserves_field_sources_and_variant_candidates(tmp_path):
    dump(tmp_path/'data/detail_outputs.json', [
        dict(page=51, detail_id='d', title='Thermal section', system={'type': 'CC', 'evidence':['printed heading']},
             article_database=[dict(profile_id='235918', occurrence_id='entity:a', label='Nose adaptor',
                                    depth={'value':14.5,'evidence':[{'page':51,'bbox':[.1,.2,.3,.4]}]},
                                    conflicts=[], missing_information=[])],
             assembly_database=[], unresolved=[])])
    dump(tmp_path/'data/article_database.json', [
        dict(profile_id='235918', table_observations=[
            dict(provenance={'pdf_page':62}, fields=[{'header':'Shape','value':'18.5mm'}]),
            dict(provenance={'pdf_page':62}, fields=[{'header':'Shape','value':'14.5mm'}])])])
    dump(tmp_path/'data/relationships.json', [])
    result = page_explanation(tmp_path,51)
    assert result['details'][0]['articles'][0]['facts']['depth']['value'] == 14.5
    assert result['details'][0]['articles'][0]['facts']['depth']['evidence'][0]['page'] == 51
    assert len(result['catalogue_candidates']['235918']) == 2



def test_direct_model_trace_contains_prompt_and_receipt(tmp_path, monkeypatch):
    import pytest
    mlflow = pytest.importorskip('mlflow')
    from facade_dspy.openai_api import OpenAIResponses

    class Span:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def set_inputs(self, inputs): self.inputs=inputs
        def set_outputs(self, outputs): self.outputs=outputs
        def set_attributes(self, attributes): self.attributes=attributes

    span = Span()
    monkeypatch.setattr(mlflow, 'get_current_active_span', lambda: object())
    monkeypatch.setattr(mlflow, 'start_span', lambda **kwargs: span)
    backend = OpenAIResponses.__new__(OpenAIResponses)
    backend.model = 'gpt-6.1-sol'
    reply = dict(output={'answer':'from page 51'}, usage={'input_tokens':10,'output_tokens':5,'total_tokens':15},
                 reported_cost_usd=.001)
    monkeypatch.setattr(backend, '_execute', lambda *args: reply, raising=False)
    assert backend.execute('extraction','Actual reading instructions',{'type':'object'},tmp_path,lambda:None)==reply
    assert span.inputs['prompt']=='Actual reading instructions'
    assert span.outputs==reply['output']
    assert span.attributes['mlflow.chat.tokenUsage']['total_tokens']==15
