"""Unleash Admin API client: flag inventory + usage signals.

Staleness signals (verified semantics):
- lastSeenAt: when metrics were last collected for a flag (null = never).
- last-hour usage: per-feature evaluation counts reported by client SDKs.
Historical usage over arbitrary windows is NOT reliably queryable in OSS
Unleash, so the reaper uses recency (lastSeenAt) + last-hour counts.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import requests

DEFAULT_URL = os.environ.get("UNLEASH_URL", "http://localhost:4242")
DEFAULT_TOKEN = os.environ.get(
    "UNLEASH_ADMIN_TOKEN",
    "*:*.b8f8c6a1d2e34f5a9c7b0d8e1f2a3b4c5d6e7f8a9b0c1d2e",
)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": token, "Content-Type": "application/json"}


def _admin(url: str, token: str) -> str:
    return url.rstrip("/") + "/api/admin"


def list_flags(url: str = DEFAULT_URL, token: str = DEFAULT_TOKEN) -> list[dict]:
    """All feature toggles in the default project with derived state.

    Verified against Unleash v8: the legacy GET /api/admin/features endpoint is
    gone; the project-scoped endpoint is canonical. lastSeenAt lives on each
    environment entry (null until a client reports metrics).
    """
    resp = requests.get(
        f"{_admin(url, token)}/projects/default/features",
        headers=_headers(token), timeout=15,
    )
    resp.raise_for_status()
    out = []
    for f in resp.json().get("features", []):
        envs = f.get("environments") or []
        default_env = next((e for e in envs if e.get("name") == "development"), None)
        seen = [e.get("lastSeenAt") for e in envs if e.get("lastSeenAt")]
        out.append(
            {
                "name": f["name"],
                "type": f.get("type"),
                "project": f.get("project", "default"),
                "created_at": f.get("createdAt"),
                # current evaluation default in the development environment
                "default_enabled": bool(default_env.get("enabled")) if default_env else False,
                "last_seen_at": max(seen) if seen else None,
                "environments": {e["name"]: {"enabled": e.get("enabled", False)} for e in envs},
            }
        )
    return out


def last_hour_usage(
    flag_name: str, project: str = "default", url: str = DEFAULT_URL, token: str = DEFAULT_TOKEN
) -> int:
    """Evaluation count in the last hour (yes+no); 0 if none.

    Verified against Unleash v8: GET /api/admin/client-metrics/features/{name}
    returns {lastHourUsage: [{environment, timestamp, yes, no}], seenApplications}.
    Counts appear with a short aggregation delay after clients flush.
    """
    try:
        resp = requests.get(
            f"{_admin(url, token)}/client-metrics/features/{flag_name}",
            headers=_headers(token), timeout=10,
        )
        if resp.status_code != 200:
            return 0
        data = resp.json()
        return sum(int(e.get("yes", 0)) + int(e.get("no", 0))
                   for e in data.get("lastHourUsage", []))
    except (requests.RequestException, ValueError):
        return 0


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_recently_seen(flag: dict, window_days: int) -> bool:
    seen = _parse_ts(flag.get("last_seen_at"))
    if not seen:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return seen >= datetime.now(timezone.utc) - timedelta(days=window_days)


def age_hours(flag: dict) -> float:
    created = _parse_ts(flag.get("created_at"))
    if not created:
        return float("inf")
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 3600.0)
