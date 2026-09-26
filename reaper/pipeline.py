"""Scan orchestration + shared state. One scan = inventory → trace → classify."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from . import audit, classify, inventory as inv, prs, tracer

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = Path(os.environ.get("REAPER_STATE_PATH", _PROJECT_ROOT / "audit" / "state.json"))

_DEFAULT_REPO = os.environ.get(
    "REAPER_REPO", str(_PROJECT_ROOT / "demo" / "checkout-service")
)

_LOCK = threading.Lock()
_state: dict = {"flags": {}, "scanned_at": None}


def default_repo() -> str:
    return _DEFAULT_REPO


def scan(repo: str | None = None, min_age_hours: float = 0.0,
         window_days: int = 60) -> dict:
    repo = repo or default_repo()
    audit.log("agent", "scan_started", {"repo": repo, "min_age_hours": min_age_hours,
                                        "window_days": window_days})
    results = {}
    for flag in inv.list_flags():
        usage = inv.last_hour_usage(flag["name"], flag.get("project", "default"))
        trace = tracer.trace(repo, flag["name"])
        verdict = classify.classify(
            flag, trace, last_hour_count=usage,
            min_age_hours=min_age_hours, window_days=window_days, repo=repo,
        )
        verdict.update({
            "flag": flag["name"],
            "created_at": flag.get("created_at"),
            "last_seen_at": flag.get("last_seen_at"),
            "last_hour_count": usage,
            "default_enabled": flag.get("default_enabled", False),
        })
        results[flag["name"]] = verdict

    with _LOCK:
        _state["flags"] = results
        _state["scanned_at"] = audit.log("agent", "scan_completed",
                                         {"flags": len(results)})["ts"]
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(_state, indent=2))
    return build_report()


def get_state() -> dict:
    if not _state["flags"] and STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return dict(_state)


def record_answer(flag_key: str, answer: str, answered_by: str = "human") -> dict:
    """Gate resolution for an UNKNOWN flag: 'in-use' | 'dead'."""
    with _LOCK:
        state = get_state()
        flags = state.get("flags", {})
        entry = flags.get(flag_key)
        if not entry:
            raise KeyError(f"unknown flag: {flag_key}")
        audit.log(answered_by, "unknown_answered", {"flag": flag_key, "answer": answer})
        if answer in ("in-use", "live", "still-live"):
            entry["verdict"] = "STILL_LIVE"
            entry["reason"] = "human-said-in-use"
            entry["detail"] = "human confirmed the flag is still in use; report only"
        elif answer in ("dead", "remove", "removable"):
            entry["verdict"] = "REMOVABLE"
            entry["reason"] = "human-confirmed-dead"
            entry["removal_tier"] = "flag-only"
            entry["detail"] = "human confirmed the flag is dead; safe to remove (plan-only)"
            entry.pop("question", None)
        else:
            raise ValueError("answer must be 'in-use' or 'dead'")
        entry["resolved_by"] = answered_by
        STATE_PATH.write_text(json.dumps(state, indent=2))
        return entry


def build_report() -> dict:
    state = get_state()
    flags = state.get("flags", {})
    summary = {"REMOVABLE": 0, "STILL_LIVE": 0, "UNKNOWN": 0, "SKIP": 0}
    for entry in flags.values():
        summary[entry["verdict"]] = summary.get(entry["verdict"], 0) + 1
    return {
        "scanned_at": state.get("scanned_at"),
        "summary": summary,
        "flags": sorted(flags.values(), key=lambda e: (e["verdict"], e["flag"])),
        "pr_configured": prs.available(),
    }
