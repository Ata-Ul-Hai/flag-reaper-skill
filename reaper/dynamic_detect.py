"""Deterministic detection of dynamically-constructed flag keys.

Catches (without any LLM):
- f-strings that embed a key fragment:        f"exp_{name}"
- string concatenation building the key:     "feature_" + env
- keys defined only in config maps:          flags.yaml / .json / .toml entries
"""
from __future__ import annotations

import re

CONFIG_EXTS = {".yaml", ".yml", ".json", ".toml", ".ini", ".properties"}

# f"...frag..."  or f'...frag...'
_FSTRING = [
    (r'f"[^"\n]*{fragment}[^"\n]*"', '"'),
    (r"f'[^'\n]*{fragment}[^'\n]*'", "'"),
]
# "...frag" + <expr>   or   <expr> + "...frag..."
_CONCAT = [
    (r'"[^"\n]*{fragment}[^"\n]*"\s*\+', '"'),
    (r"'[^'\n]*{fragment}[^'\n]*'\s*\+", "'"),
    (r'\+\s*"[^"\n]*{fragment}[^"\n]*"', '"'),
    (r"\+\s*'[^'\n]*{fragment}[^'\n]*'", "'"),
]


def fragments(flag_key: str) -> list[str]:
    """Candidate fragments of a composite key, longest first."""
    parts = [p for p in re.split(r"[-_.]", flag_key) if p]
    seen: list[str] = []
    for size in range(len(parts), 0, -1):
        for i in range(0, len(parts) - size + 1):
            frag = "_".join(parts[i : i + size])
            if frag not in seen:
                seen.append(frag)
    return seen


def detect_dynamic(flag_key: str, path: str, line_text: str) -> dict | None:
    """Return evidence if this line constructs the key dynamically."""
    if path.endswith(tuple(CONFIG_EXTS)):
        return None  # config-map handled by the tracer via exact match
    for frag in fragments(flag_key):
        for template, quote in _FSTRING:
            if re.search(template.format(fragment=re.escape(frag)), line_text):
                return {"fragment": frag, "pattern": "f-string", "quote": quote}
        for template, quote in _CONCAT:
            if re.search(template.format(fragment=re.escape(frag)), line_text):
                return {"fragment": frag, "pattern": "string-concat", "quote": quote}
    return None
