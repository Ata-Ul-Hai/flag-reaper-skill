"""Append-only JSONL audit log. Every agent action and human decision lands here."""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

_LOCK = threading.Lock()
_AUDIT_PATH = os.environ.get(
    "REAPER_AUDIT_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "audit", "audit.jsonl"),
)


def log(actor: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Append one event. actor: agent | human | trueforge | system."""
    event = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "actor": actor,
        "action": action,
        "payload": payload or {},
    }
    os.makedirs(os.path.dirname(_AUDIT_PATH), exist_ok=True)
    with _LOCK:
        with open(_AUDIT_PATH, "a") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def tail(limit: int = 200) -> list[dict[str, Any]]:
    if not os.path.exists(_AUDIT_PATH):
        return []
    with open(_AUDIT_PATH) as fh:
        lines = fh.readlines()
    return [json.loads(line) for line in lines[-limit:]]


def reset() -> int:
    """Truncate the log (only used by `make demo-reset`)."""
    os.makedirs(os.path.dirname(_AUDIT_PATH), exist_ok=True)
    with _LOCK, open(_AUDIT_PATH, "w") as fh:
        fh.write("")
    return 0
