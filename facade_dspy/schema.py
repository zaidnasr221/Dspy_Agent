"""Schemas and structural validator adapted from the existing catalogue worker."""
import json,math,re
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field

class Record(BaseModel):
    model_config=ConfigDict(extra='forbid')

class Detail(Record):
    id:str
    title:str
    drawing_refs:list[str]
    bbox:list[float]=Field(min_length=4,max_length=4)

class Node(Record):
    id:str
    detail_id:str
    article_id:str|None
    kind:str
    label:str
    material:str|None
    quantity:float|None
    callout:str|None
    bbox:list[float]=Field(min_length=4,max_length=4)

class Relation(Record):
    source:str
    predicate:str
    target:str
    basis:Literal['explicit_text','drawing_reading','inference']
    condition:str|None
    bbox:list[float]=Field(min_length=4,max_length=4)

class Measurement(Record):
    subject:str
    property:str
    value:float|str|list[float]|None
    unit:str|None
    datum:str|None
    condition:str|None
    bbox:list[float]=Field(min_length=4,max_length=4)

class TableRow(Record):
    cells:list[str|None]
    bbox:list[float]=Field(min_length=4,max_length=4)

class Table(Record):
    id:str
    title:str
    columns:list[str]
    units:list[str|None]
    rows:list[TableRow]
    footnotes:list[str]
    bbox:list[float]=Field(min_length=4,max_length=4)

class Instruction(Record):
    detail_id:str
    sequence:int|None
    text:str
    condition:str|None
    article_refs:list[str]
    bbox:list[float]=Field(min_length=4,max_length=4)

class PageResult(Record):
    pdf_page:int
    page_kind:str
    details:list[Detail]
    nodes:list[Node]
    relations:list[Relation]
    measurements:list[Measurement]
    tables:list[Table]
    instructions:list[Instruction]
    unresolved:list[str]


def validate(result,n,drawing_patterns=None):
    obj=PageResult.model_validate(result)
    if obj.pdf_page!=n:
        raise ValueError('Wrong source page')
    details={d.id for d in obj.details}
    nodes={node.id for node in obj.nodes}
    if len(nodes)!=len(obj.nodes) or len(details)!=len(obj.details):
        raise ValueError('Duplicate local IDs')
    for node in obj.nodes:
        if node.detail_id not in details:
            raise ValueError(f'Unknown detail: {node.detail_id}')
        if node.article_id and (re.match(r'(?:geff|K\d{7}\b)',node.article_id,re.I) if drawing_patterns is None else
                                any(re.match(p+r'(?:\b|$)',node.article_id.strip(),re.I) for p in drawing_patterns)):
            raise ValueError('Drawing ID used as article')
    for rel in obj.relations:
        if rel.source not in nodes or rel.target not in nodes:
            raise ValueError(f'Relation references undefined node: {rel.source} -> {rel.target}')
    for m in obj.measurements:
        if m.subject not in nodes|details:
            raise ValueError(f'Measurement references undefined subject: {m.subject}')
    for ins in obj.instructions:
        if ins.detail_id not in details:
            raise ValueError('Instruction references undefined detail')
    for table in obj.tables:
        if len(table.columns)!=len(table.units) or any(len(row.cells)!=len(table.columns) for row in table.rows):
            raise ValueError(f'Inconsistent table width: {table.id}')
    def boxes(value):
        if isinstance(value,dict):
            if 'bbox' in value:
                a,b,c,d=value['bbox']
                if not all(math.isfinite(x) for x in [a,b,c,d]) or not(0<=a<=c<=1 and 0<=b<=d<=1):
                    raise ValueError('Invalid source bounding box')
            for v in value.values():boxes(v)
        elif isinstance(value,list):
            for v in value:boxes(v)
    boxes(obj.model_dump())
    def finite(value):
        if isinstance(value,float) and not math.isfinite(value):raise ValueError('Non-finite numeric value')
        if isinstance(value,dict):
            for v in value.values():finite(v)
        elif isinstance(value,list):
            for v in value:finite(v)
    finite(obj.model_dump())
    if len({t.id for t in obj.tables})!=len(obj.tables):raise ValueError('Duplicate table IDs within one region')
    if re.search('[\u0600-\u06ff]',json.dumps(obj.model_dump(),ensure_ascii=False)):
        raise ValueError('Non-English output')
    return obj.model_dump()


class SourceText(Record):
    text:str
    bbox:list[float]=Field(min_length=4,max_length=4)


class VisualResult(Record):
    unit_id:str
    source_text:list[SourceText]
    extraction:PageResult


class Identity(Record):
    manufacturer_brand:str|None
    legal_manufacturer:str|None
    system_id:str|None
    document_name:str|None
    edition:str|None
    copyright_notice:str|None
    evidence_pages:list[int]
    unresolved:list[str]


class ReviewFinding(Record):
    severity:Literal['question','error']
    message:str
    bbox:list[float]=Field(min_length=4,max_length=4)


class VisualReview(Record):
    unit_id:str
    findings:list[ReviewFinding]
    scope:str
