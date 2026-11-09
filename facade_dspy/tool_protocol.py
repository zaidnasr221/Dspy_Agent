"""Shared failures and secret redaction for independent CLI adapters."""
import os
import re

class ToolFailure(RuntimeError):pass
class IncompleteRead(ToolFailure):pass
class AuthenticationFailure(ToolFailure):pass
class InsufficientCredits(ToolFailure):pass
class RateLimitFailure(ToolFailure):pass

def redact(text):
    for name in ('OPENAI_API_KEY','ANTHROPIC_API_KEY'):
        key=os.environ.get(name)
        if key:text=text.replace(key,'[REDACTED]')
    return re.sub(r'sk-[A-Za-z0-9_-]{20,}','[REDACTED]',text)
