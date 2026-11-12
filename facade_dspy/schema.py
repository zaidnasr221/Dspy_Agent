"""Schemas and structural validator adapted from the existing catalogue worker."""
import json,math,re
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field

class Record(BaseModel):
    model_config=ConfigDict(extra='forbid')

# Generic vocabularies (manufacturer independent). 'other' is always allowed with a clarification.
SystemType=Literal['SG','CC','semi-SG']
ArticleType=Literal['bar','piece','parameter']
ProfileRole=Literal['mullion','transom','pressure_plate','cover_cap','end_cap','isolator','gasket_internal',
                    'gasket_external','structural_silicone','weather_seal','toggle','spacer','glass_support',
                    'glazing_adaptor','cleat','bracket','sleeve','screw','bolt','rivet','anchor','washer','nut',
                    'glass','infill','sealing_tape','other']
RelationType=Literal['clamped','bonded','fixed','supported','movement','insertion','other']
EvidenceStatus=Literal['explicit','visual','unresolved']
InsertionDirection=Literal['exterior_to_interior','interior_to_exterior','top_down','bottom_up','side','other']
ViewType=Literal['profile_2d','assembly_section_2d','assembly_isometric','assembly_orthographic','table']

class Detail(Record):
    id:str
    title:str
    drawing_refs:list[str]
    bbox:list[float]=Field(min_length=4,max_length=4)
    system_type:Literal['SG','CC','semi-SG']|None=None
    position:str|None=None
    view_type:ViewType|None=None

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
    article_type:Literal['bar','piece','parameter']|None=None
    finish:str|None=None
    color:str|None=None
    profile_role:ProfileRole|None=None
    role_clarification:str|None=Field(default=None,description="Required when profile_role is 'other'.")
    evidence_status:EvidenceStatus|None=None
    inference_reason:str|None=None
    missing_information:list[str]|None=None

class RelationParticipant(Record):
    node_id:str
    role:str=Field(min_length=1)

class FixingOffset(Record):
    value:float
    unit:str|None
    from_datum:str
    to_datum:str
    evidence:str
    bbox:list[float]=Field(min_length=4,max_length=4)

class Relation(Record):
    source:str
    predicate:str
    target:str
    basis:Literal['explicit_text','drawing_reading','inference']
    condition:str|None
    bbox:list[float]=Field(min_length=4,max_length=4)
    evidence:str|None=Field(default=None,description='What on the page shows this relation: quoted text, or the callout/leader/contact seen in the drawing.')
    fastener_id:str|None=None
    relation_type:RelationType|None=None
    relation_clarification:str|None=Field(default=None,description="Required when relation_type is 'other'.")
    system_part:SystemType|None=Field(default=None,description='Which system part this relation belongs to (needed in semi-SG details).')
    passes_through:list[str]|None=Field(default=None,description='Node IDs the fastener passes through, in order.')
    engages_in:str|None=Field(default=None,description='Node ID the fastener finally engages in.')
    insertion_direction:InsertionDirection|None=None
    evidence_status:EvidenceStatus|None=None
    inference_reason:str|None=None
    missing_information:list[str]|None=None
    participants:list[RelationParticipant]|None=None
    fixing_evidence:Literal['engagement','alignment','fixing_location']|None=None
    fastener_position:str|None=None
    fastener_size:str|None=None
    distance_offsets:list[FixingOffset]|None=None

class EdgeReference(Record):
    rail_end:Literal['x0','x1']|None
    measured_from:Literal['rail_end','dimension_line']
    offset:float|None=None
    offset_unit:str|None=None

class Measurement(Record):
    subject:str
    property:str
    value:float|str|list[float]|None
    unit:str|None
    datum:str|None
    condition:str|None
    bbox:list[float]=Field(min_length=4,max_length=4)
    evidence:str|None=None
    rail:str|None=None
    edge_reference:EdgeReference|None=None
    bound:Literal['exact','minimum','maximum','interval']|None=None
    per:str|None=None
    per_node_id:str|None=None

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


def _drawing_patterns():
    from . import rules
    return rules.DRAWING_REFERENCE_PATTERNS


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
        # Drawing-ID patterns come from configuration (rules.py); none are hard-coded here.
        patterns=_drawing_patterns() if drawing_patterns is None else drawing_patterns
        if node.article_id and any(re.match(p+r'(?:\b|$)',node.article_id.strip(),re.I) for p in patterns):
            raise ValueError('Drawing ID used as article')
    for rel in obj.relations:
        if rel.source not in nodes or rel.target not in nodes:
            raise ValueError(f'Relation references undefined node: {rel.source} -> {rel.target}')
        for ref in [*(rel.passes_through or []),*([rel.engages_in] if rel.engages_in else []),
                    *([rel.fastener_id] if rel.fastener_id else [])]:
            if ref not in nodes:
                raise ValueError(f'Relation fastener path references undefined node: {ref}')
        if any(p.node_id not in nodes for p in rel.participants or []):
            raise ValueError('Relation participant references undefined node')
    for m in obj.measurements:
        if m.subject not in nodes|details:
            raise ValueError(f'Measurement references undefined subject: {m.subject}')
        if m.per_node_id is not None and m.per_node_id not in nodes:
            raise ValueError(f'Measurement quantity references undefined node: {m.per_node_id}')
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


class RelationVerdict(Record):
    claim_id:str|None=None
    source:str
    predicate:str
    target:str
    verdict:Literal['supported','unsupported','uncertain']
    reason:str
    bbox:list[float]=Field(min_length=4,max_length=4)
    observed_contact:bool|None=None
    observed_insertion_target:str|None=None


class RelationCheck(Record):
    unit_id:str
    verdicts:list[RelationVerdict]


# ---------------------------------------------------------------------------------------------
# Output contract: ARTICLE DATABASE and ASSEMBLY DATABASE (one file per detail or per catalogue).
# Every value carries its evidence; unknown values stay null and are listed in missing_information.

class SourceRef(Record):
    page:int
    kind:Literal['detail','table','profile_drawing','text','other']
    bbox:list[float]|None=Field(default=None,min_length=4,max_length=4)
    text:str|None=None


class Value(Record):
    value:float|str|list[float]|bool|None
    unit:str|None=None
    bound:Literal['exact','minimum','maximum','interval']|None=None
    condition:str|None=None
    datum:str|None=None
    evidence:list[SourceRef]=Field(default_factory=list)


class EdgeSpacing(Value):
    """End-to-centre distance from a rail end (x0 or x1) to the centre of the nearest article.
    When the catalogue measures to a dimension line, the line offset is added or subtracted."""
    edge_reference:EdgeReference|None=None
    rail:str|None=None
    printed_value:float|list[float]|str|None=Field(default=None,description='Value as printed, before the dimension-line offset.')
    offset_applied:Literal['added','subtracted','none']|None=None


class Spacing(Value):
    """Centre-to-centre distance between neighbouring articles on the same rail."""
    rail:str|None=None


class Quantity(Value):
    per:str|None=None
    per_article:str|None=None
    per_occurrence_id:str|None=None


class ArticleRecord(Record):
    profile_id:str|None=Field(description='Article ID as printed; null when no article ID is printed.')
    label:str|None=None
    occurrence_id:str|None=None
    profile_role:ProfileRole
    role_clarification:str|None=None
    article_type:ArticleType|None
    system:list[SystemType]=Field(default_factory=list)
    depth:Value|None=None
    edge_spacing:EdgeSpacing|None=None
    spacing:Spacing|None=None
    quantity:Quantity|None=None
    material:Value|None=None
    finish:Value|None=None
    color:Value|None=None
    other:dict[str,Value]=Field(default_factory=dict,description='All else found about this article.')
    sources:list[SourceRef]=Field(default_factory=list)
    conflicts:list[str]=Field(default_factory=list)
    evidence_status:EvidenceStatus
    inference_reason:str|None=None
    missing_information:list[str]=Field(default_factory=list)


class Participant(Record):
    article:str=Field(description='profile_id, or the label of an article without an ID.')
    role:str
    occurrence_id:str|None=None

class DistanceOffset(Value):
    from_datum:str|None=None
    to_datum:str|None=None


class FastenerAssignment(Record):
    article_id:str|None
    type:str|None
    size:str|None=None
    occurrence_id:str|None=None
    passes_through:list[str]=Field(default_factory=list)
    path_occurrence_ids:list[str]=Field(default_factory=list)
    engages_in:str|None=None
    engages_in_occurrence_id:str|None=None
    insertion_direction:InsertionDirection|None=None
    position:str|None=None
    distance_offset:list[DistanceOffset]=Field(default_factory=list)
    edge_spacing:EdgeSpacing|None=None
    spacing:Spacing|None=None
    quantity:Quantity|None=None


class AssemblyRecord(Record):
    assembly_id:str
    detail_id:str
    page:int
    view_type:ViewType|None=None
    system_part:SystemType|None=None
    relation:RelationType
    relation_clarification:str|None=None
    participants:list[Participant]=Field(min_length=2)
    fastener_assigned:FastenerAssignment|None=Field(default=None,description="Only when relation is 'fixed'.")
    position:str|None=None
    orientation:str|None=None
    sequence:list[str]=Field(default_factory=list)
    conditions:list[str]=Field(default_factory=list)
    cross_references:list[SourceRef]=Field(default_factory=list)
    other:dict[str,Value]=Field(default_factory=dict,description='All else found about this relation.')
    evidence:list[SourceRef]=Field(min_length=1)
    evidence_status:EvidenceStatus
    inference_reason:str|None=None
    missing_information:list[str]=Field(default_factory=list)


class DetailSystem(Record):
    type:SystemType|None
    evidence:list[str]=Field(default_factory=list)
    evidence_status:EvidenceStatus
    declared_type:SystemType|None=None
    per_axis:dict[Literal['vertical','horizontal'],SystemType]|None=Field(
        default=None,description='semi-SG only: which system each frame axis uses (e.g. vertical CC, horizontal SG).')


class DetailOutput(Record):
    catalogue:str
    page:int
    detail_id:str
    title:str|None=None
    view_type:ViewType|None=None
    position:str|None=None
    system:DetailSystem
    article_database:list[ArticleRecord]
    assembly_database:list[AssemblyRecord]
    unresolved:list[str]=Field(default_factory=list)


def validate_output(value):
    """Structural rules of the output contract that a JSON schema alone cannot express."""
    obj=DetailOutput.model_validate(value)
    known={a.profile_id for a in obj.article_database if a.profile_id}|{a.label for a in obj.article_database if a.label}
    occurrences={a.occurrence_id: {a.profile_id, a.label} for a in obj.article_database if a.occurrence_id}
    for article in obj.article_database:
        if article.profile_role=='other' and not (article.role_clarification or '').strip():
            raise ValueError("profile_role 'other' needs role_clarification")
        if article.evidence_status=='visual' and not (article.inference_reason or '').strip():
            raise ValueError('Visual evidence needs inference_reason')
    for item in obj.assembly_database:
        if item.relation=='other' and not (item.relation_clarification or '').strip():
            raise ValueError("relation 'other' needs relation_clarification")
        if item.fastener_assigned is not None and item.relation!='fixed':
            raise ValueError("fastener_assigned is only allowed when relation is 'fixed'")
        if item.evidence_status=='visual' and not (item.inference_reason or '').strip():
            raise ValueError('Visual evidence needs inference_reason')
        if obj.system.type=='semi-SG' and item.system_part is None:
            raise ValueError('semi-SG details need system_part on every relation')
        refs=[p.article for p in item.participants]
        for participant in item.participants:
            if participant.occurrence_id and participant.article not in occurrences.get(participant.occurrence_id, set()):
                raise ValueError('Participant occurrence does not match its article')
        if item.fastener_assigned:
            fastener=item.fastener_assigned
            refs+=fastener.passes_through+([fastener.engages_in] if fastener.engages_in else [])
            if fastener.occurrence_id and fastener.occurrence_id not in occurrences:
                raise ValueError('Unknown fastener occurrence')
            if fastener.path_occurrence_ids and (len(fastener.path_occurrence_ids)!=len(fastener.passes_through) or any(
                    ref not in occurrences.get(ident, set()) for ref,ident in zip(fastener.passes_through,fastener.path_occurrence_ids))):
                raise ValueError('Fastener path occurrence does not match its article')
            if fastener.engages_in_occurrence_id and fastener.engages_in not in occurrences.get(fastener.engages_in_occurrence_id, set()):
                raise ValueError('Fastener engagement occurrence does not match its article')
        for ref in refs:
            if ref not in known:
                raise ValueError(f'Relation references unknown article: {ref}')
    return obj.model_dump()


# ---------------------------------------------------------------------------------------------
# Catalogue deliverables (classification process): ARTICLE DB = one record per article across the
# whole catalogue; ASSEMBLY DB = every verified relation. Field order follows the deliverables list.

class Occurrence(Record):
    page:int
    detail_id:str
    occurrence_id:str|None=None
    label:str|None=None
    view_type:ViewType|None=None


class CatalogueArticle(Record):
    profile_id:str|None=Field(description='Article ID as printed; null only for a part shown without an ID.')
    profile_role:ProfileRole
    role_clarification:str|None=None
    article_type:ArticleType|None=Field(description='Fabrication (FWR) type: bar, piece or parameter.')
    depth:Value|None=None
    edge_spacing:EdgeSpacing|None=Field(default=None,description='Pieces only.')
    spacing:Spacing|None=Field(default=None,description='Pieces only.')
    quantity:Quantity|None=None
    material:Value|None=None
    finish:Value|None=None
    color:Value|None=None
    other:dict[str,Value]=Field(default_factory=dict,description='All else found.')
    label:str|None=None
    systems:list[SystemType]=Field(default_factory=list)
    occurrences:list[Occurrence]=Field(min_length=1)
    sources:list[SourceRef]=Field(default_factory=list)
    conflicts:list[str]=Field(default_factory=list)
    evidence_status:EvidenceStatus
    missing_information:list[str]=Field(default_factory=list)


class CatalogueAssembly(AssemblyRecord):
    catalogue:str
    detail_title:str|None=None


class ArticleDB(Record):
    catalogue:str
    articles:list[CatalogueArticle]


class AssemblyDB(Record):
    catalogue:str
    assemblies:list[CatalogueAssembly]


def validate_deliverables(article_db, assembly_db):
    """Process rules the JSON schema cannot express: pieces-only spacing, unique article IDs,
    fastener only on 'fixed', participants must exist in the Article DB."""
    articles=ArticleDB.model_validate(article_db)
    assemblies=AssemblyDB.model_validate(assembly_db)
    ids=[a.profile_id for a in articles.articles if a.profile_id]
    if len(ids)!=len(set(ids)):
        raise ValueError('Article DB has duplicate profile_id records')
    known=set(ids)|{a.label for a in articles.articles if a.label}
    for article in articles.articles:
        if article.article_type not in (None,'piece') and (article.edge_spacing or article.spacing):
            raise ValueError(f'edge_spacing/spacing only belong to pieces: {article.profile_id or article.label}')
    for item in assemblies.assemblies:
        if item.fastener_assigned is not None and item.relation!='fixed':
            raise ValueError("fastener_assigned is only allowed when relation is 'fixed'")
        if item.relation=='other' and not (item.relation_clarification or '').strip():
            raise ValueError("relation 'other' needs relation_clarification")
        for participant in item.participants:
            if participant.article not in known:
                raise ValueError(f'Assembly participant not in Article DB: {participant.article}')
    return articles.model_dump(),assemblies.model_dump()
