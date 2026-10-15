"""Last line of defence: the agent already strips secrets, but the backend never trusts that."""
import re

SENSITIVE_KEY = re.compile(r"pass(word|wd)?|secret|token|credential|private_?key|api_?key", re.I)
# Keys produced by the agent/backend that only say whether a password exists
SAFE_KEYS = {"passwordSet", "passwordProtection"}


def redact(value):
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items() if k in SAFE_KEYS or not SENSITIVE_KEY.search(k)}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value
