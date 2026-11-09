from pathlib import Path
import hashlib

import pymupdf
import pytest

from facade_dspy import rules
from facade_dspy.program import FacadeExtractionAgent
from facade_dspy.source import content_box
from facade_dspy.storage import read
from test_agent import FakeBackend, pdf_file


def test_grounding_requires_a_complete_article_token():
    assert rules.grounded('235918', ['Isolator 235 918 (20.5mm)'])
    assert rules.grounded('A100', ['Article A100.'])
    assert not rules.grounded('235918', ['Only 235 918-20.5 is shown'])
    assert not rules.grounded('A100', ['Other parts XA1000 and A1000'])


def test_content_bounds_follow_page_rotation():
    with pymupdf.open() as doc:
        page=doc.new_page(width=400,height=600)
        page.draw_rect(pymupdf.Rect(30,50,95,110),color=(0,0,0))
        page.set_rotation(90)
        box=content_box(page)
        assert box[0]>.79 and box[2]>.90
        assert box[1]<.09 and box[3]<.28


def test_page_filling_raster_is_kept_when_there_are_small_overlays():
    with pymupdf.open() as doc:
        page=doc.new_page(width=400,height=600)
        pixels=pymupdf.Pixmap(pymupdf.csRGB,pymupdf.IRect(0,0,40,60),False)
        pixels.clear_with(200)
        page.insert_image(page.rect,pixmap=pixels)
        page.insert_text((30,50),'Overlay')
        assert content_box(page)==[0,0,1,1]


def test_legacy_verdicts_cannot_confirm_an_ambiguous_claim():
    relation={'source':'a','predicate':'supports','target':'b'}
    legacy=dict(relation,verdict='supported')
    assert rules.relation_verdicts([relation],[legacy])=={'r1':'supported'}
    assert rules.relation_verdicts([relation,relation],[legacy])=={}
    assert rules.relation_verdicts([relation],[dict(legacy,claim_id='r1'),
        dict(legacy,claim_id='r1',verdict='unsupported')])=={'r1':'unsupported'}


@pytest.mark.parametrize('name',['budget.py','model.py'])
def test_checkpoint_accepts_equivalent_line_endings(name):
    from facade_dspy.checkpoint_compat import compatible_code
    code=(Path(__file__).resolve().parents[1]/'facade_dspy'/name).read_bytes().replace(b'\r\n',b'\n')
    lf,crlf=(hashlib.sha256(value).hexdigest() for value in (code,code.replace(b'\n',b'\r\n')))
    assert compatible_code('code:'+name,lf,crlf)
    assert compatible_code('code:'+name,crlf,lf)
    assert not compatible_code('code:'+name,'0'*64,crlf)


def test_distinct_claims_with_the_same_endpoints_keep_separate_verdicts_on_replay(tmp_path):
    class VariantBackend(FakeBackend):
        def execute(self,task,prompt,schema,folder,heartbeat=lambda:None):
            reply=super().execute(task,prompt,schema,folder,heartbeat)
            if task=='extraction':
                rows=reply['output']['extraction']['relations']
                rows.append(dict(rows[0],condition='Alternate assembly',evidence='Different callout'))
            elif task=='relation_check':
                claims=read(folder/'relations.json')
                for i,(verdict,claim) in enumerate(zip(reply['output']['verdicts'],claims)):
                    verdict['verdict']='unsupported' if i==0 else 'supported'
                    if 'claim_id' in claim:
                        verdict['claim_id']=claim['claim_id']
            return reply

    pdf=pdf_file(tmp_path/'fixture.pdf')
    result=FacadeExtractionAgent(out=tmp_path/'live',budget_usd=1,
        backend_factory=lambda r,b:VariantBackend([]))(pdf_url=pdf.as_uri())
    live=read(Path(result.snapshot_directory)/'data/relationships.json')
    assert {row['relationship_status'] for row in live if row['condition']=='Fixture only'}=={'invalid'}
    assert {row['relationship_status'] for row in live if row['condition']=='Alternate assembly'}=={'proposed'}
    replay=FacadeExtractionAgent(out=tmp_path/'replay',reuse=result.output_directory,replay=True)(pdf_url=pdf.as_uri())
    assert read(Path(replay.snapshot_directory)/'data/relationships.json')==live


@pytest.mark.parametrize('backend', ['opencode','claude'])
def test_review_adapters_require_the_claim_file_before_launch(tmp_path,monkeypatch,backend):
    from facade_dspy.budget import Budget
    from facade_dspy.claude_tool import ClaudeCode
    from facade_dspy.opencode_tool import OpenCode
    from facade_dspy.tool_protocol import ToolFailure
    from test_backends import fake_cli

    monkeypatch.setenv('OPENAI_API_KEY','offline-openai-fixture-not-a-real-key')
    monkeypatch.setenv('ANTHROPIC_API_KEY','offline-anthropic-fixture-not-a-real-key')
    folder=tmp_path/'inputs'
    folder.mkdir()
    for name in ('source.png','focus.png'):
        (folder/name).write_bytes(b'fixture')
    adapter={'opencode':OpenCode,'claude':ClaudeCode}[backend](
        tmp_path,Budget(tmp_path,1,.5),command=fake_cli(tmp_path,backend),accept_estimates=True)
    with pytest.raises(ToolFailure,match='relations.json'):
        adapter.execute('relation_check','Check relations',{'type':'object'},folder)
    assert not (tmp_path/'cost_ledger.json').exists()
