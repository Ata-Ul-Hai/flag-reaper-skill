"""Ordered classifier — first match wins (AGENT.md §5)."""
from __future__ import annotations

from typing import Any

from . import inventory as inv
from . import remover


def classify(flag: dict, trace_result: dict, last_hour_count: int = 0,
             min_age_hours: float = 72.0, window_days: int = 60,
             repo: str = ".") -> dict[str, Any]:
    """Return {verdict, reason, evidence, removal_tier, question?}.

    verdicts: SKIP | STILL_LIVE | UNKNOWN | REMOVABLE
    """
    refs = trace_result["references"]
    dyn = trace_result["dynamic_candidates"]
    counts = trace_result["counts"]
    key = flag["name"]

    def evidence() -> list[dict]:
        items = [
            {"path": r["path"], "line": r["line"], "text": r["text"][:200], "kind": r["kind"]}
            for r in refs
        ]
        items += [
            {"path": d["path"], "line": d["line"], "text": d["text"][:200], "kind": "dynamic",
             "fragment": d["fragment"], "pattern": d["pattern"]}
            for d in dyn
        ]
        return items

    # Rule 0 — too young to judge
    if inv.age_hours(flag) < min_age_hours:
        return {"verdict": "SKIP", "reason": "too-young",
                "detail": f"flag is {inv.age_hours(flag):.1f}h old (< {min_age_hours}h)",
                "evidence": [], "removal_tier": None}

    # Rule 1 — real evaluation traffic
    if last_hour_count > 0 or inv.is_recently_seen(flag, window_days):
        return {"verdict": "STILL_LIVE", "reason": "traffic",
                "detail": f"lastSeenAt={flag.get('last_seen_at')}, last-hour evaluations={last_hour_count}",
                "evidence": evidence(), "removal_tier": None}

    # Rule 2 — dynamic key construction can't be proven dead
    if dyn or counts["config_map"] > 0:
        if dyn:
            d = dyn[0]
            question = (f"`{key}` appears to be constructed at runtime "
                        f"({d['pattern']} using fragment '{d['fragment']}') in "
                        f"{d['path']}:{d['line']} — is this flag still in use?")
        else:
            r = next(r for r in refs if r["kind"] == "config-map")
            question = (f"`{key}` is defined in a config map at {r['path']}:{r['line']} "
                        f"and looked up at runtime — is this flag still in use?")
        return {"verdict": "UNKNOWN", "reason": "dynamic-reference",
                "detail": question, "question": question,
                "evidence": evidence(), "removal_tier": None}

    # Rule 3 — no references anywhere
    if not refs:
        return {"verdict": "REMOVABLE", "reason": "no-references",
                "detail": "no code references found; delete the flag in Unleash",
                "evidence": [], "removal_tier": "flag-only"}

    # Rule 4 — referenced only by tests
    if refs and all(r["kind"] == "test" for r in refs):
        return {"verdict": "REMOVABLE", "reason": "tests-only",
                "detail": "all references are in test files; remove tests + flag",
                "evidence": evidence(), "removal_tier": "tests-and-flag"}

    # Rule 5 — every code call site is literal and trivially removable
    # (test-file references are fine: the suite is re-run after the patch)
    if all(r["kind"] in ("literal", "test") for r in refs) and counts["literal"] > 0:
        plan = remover.plan_trivial_removal(
            key, flag.get("default_enabled", False),
            [r for r in refs if r["kind"] == "literal"], repo=repo,
        )
        if plan is not None:
            return {"verdict": "REMOVABLE", "reason": "trivial-literal",
                    "detail": "all references are simple flag-check conditions; "
                              "keep the branch matching the default value",
                    "evidence": evidence(), "removal_tier": "code-and-flag",
                    "plan": plan}
        return {"verdict": "STILL_LIVE", "reason": "non-trivial-references",
                "detail": "references exist but removal is non-trivial; report only",
                "evidence": evidence(), "removal_tier": None}

    # Rule 6 — anything else: be conservative
    return {"verdict": "STILL_LIVE", "reason": "mixed-references",
            "detail": "references found in code; treating as live (conservative)",
            "evidence": evidence(), "removal_tier": None}
