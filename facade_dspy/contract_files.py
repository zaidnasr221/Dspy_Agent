"""Write the JSON Schema files in schemas/ from the Pydantic models (single source of truth).

    python -m facade_dspy.contract_files [schemas_dir]
"""
import json
import sys
from pathlib import Path

from .schema import ArticleDB, AssemblyDB, AssemblyRecord, ArticleRecord, DetailOutput, PageResult

CONTRACTS = {
    'page_result': PageResult,
    'article_record': ArticleRecord, 'assembly_record': AssemblyRecord, 'detail_output': DetailOutput,
    'article_db': ArticleDB, 'assembly_db': AssemblyDB,
}


def render(model):
    return json.dumps(model.model_json_schema(), indent=2, ensure_ascii=False) + '\n'


def write(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, model in CONTRACTS.items():
        (directory / f'{name}.schema.json').write_text(render(model), encoding='utf-8', newline='\n')
    return sorted(CONTRACTS)


if __name__ == '__main__':
    print(write(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / 'schemas'))
