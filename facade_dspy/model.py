"""Single source of truth for backend/model defaults, selection and factories."""
from dataclasses import asdict,dataclass
import os
import re

DEFAULT_BACKEND='opencode'
DEFAULT_MODELS={'claude-code':'claude-sonnet-5-5','opencode':'openai/gpt-6.1-sol'}
API_KEY_ENV={'claude-code':'ANTHROPIC_API_KEY','opencode':'OPENAI_API_KEY'}


@dataclass(frozen=True)
class ModelConfig:
    backend:str
    model:str
    api_key_env:str

    def as_dict(self):return asdict(self)


def resolve_model(backend=None,model=None):
    backend=(backend or os.environ.get('FACADE_BACKEND') or DEFAULT_BACKEND).strip()
    if backend not in DEFAULT_MODELS:raise ValueError('Unknown backend; choose '+', '.join(DEFAULT_MODELS))
    model=(model or os.environ.get('FACADE_MODEL') or DEFAULT_MODELS[backend]).strip()
    if not model or re.search(r'\s',model):raise ValueError('Model must be a non-empty identifier without whitespace')
    if backend=='opencode' and not re.fullmatch(r'openai/[A-Za-z0-9_.:-]+',model):
        raise ValueError('OpenCode uses the OpenAI API: select a vision/tool-capable openai/<model>')
    if backend=='claude-code' and model.startswith('openai/'):
        raise ValueError('An openai/<model> requires the opencode backend')
    return ModelConfig(backend,model,API_KEY_ENV[backend])


def make_backend_factory(config,command=None,cli_budget=.15,accept_estimates=False):
    """Keep the existing (run, budget) factory injection contract."""
    def factory(run,budget):
        if config.backend=='claude-code':
            from .claude_tool import ClaudeCode
            return ClaudeCode(run,budget,command,config.model,cli_budget,accept_estimates=accept_estimates)
        from .opencode_tool import OpenCode
        return OpenCode(run,budget,command=command,model=config.model,accept_estimates=accept_estimates)
    return factory


def add_model_arguments(parser):
    parser.add_argument('--backend',choices=tuple(DEFAULT_MODELS),help='Backend override; otherwise FACADE_BACKEND or '+DEFAULT_BACKEND)
    parser.add_argument('--model',help='Model override; otherwise FACADE_MODEL or the backend default in model.py')
    parser.add_argument('--backend-executable',help='Native executable for the selected backend; never a shell command')
