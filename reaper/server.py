"""Feature Flag Reaper — MCP tool server (FastMCP).

Deterministic tools wrapped around the reaper core. TrueForge attaches this
server by URL and runs the agent loop; every tool call is written to the
append-only audit log.

Also serves the thin backup views (demo insurance only):
  GET /report  — verdict table with evidence
  GET /audit   — the full JSONL trail
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from fastmcp import FastMCP
from starlette.responses import JSONResponse

from reaper import audit, pipeline, prs, remover, tracer

mcp: FastMCP = FastMCP(
    name="reaper",
    instructions=(
        "Feature Flag Reaper: hunts zombie feature flags in a real Unleash "
        "instance and a real git repo. Follow the reaper-runbook skill "
        "exactly. Never guess on UNKNOWN flags — ask the human. You cannot "
        "merge; the open_pr tool only opens a pull request."
    ),
)

# where apply_and_test leaves its disposable working copies
_WORK_ROOT = Path(tempfile.gettempdir()) / "reaper-work"


def _flag_entry(flag_key: str) -> dict:
    entry = pipeline.get_state().get("flags", {}).get(flag_key)
    if not entry:
        raise ValueError(f"no scan result for '{flag_key}' — run classify_flags first")
    return entry


# ---------------------------------------------------------------------------


@mcp.tool
def inventory_flags() -> str:
    """List all feature flags from the Unleash Admin API with usage signals
    (created_at, lastSeenAt, last-hour evaluations, default value)."""
    from reaper import inventory as inv

    flags = inv.list_flags()
    for f in flags:
        f["last_hour_count"] = inv.last_hour_usage(f["name"], f.get("project", "default"))
    audit.log("agent", "inventory_flags", {"count": len(flags)})
    return json.dumps({"flags": flags}, indent=1)


@mcp.tool
def trace_references(flag_key: str) -> str:
    """Trace a flag key through the git repo with ripgrep: exact references by
    kind (literal / test / config-map / comment) plus dynamic-construction
    candidates (f-strings, string concat, config lookups)."""
    result = tracer.trace(pipeline.default_repo(), flag_key)
    audit.log("agent", "trace_references", {"flag": flag_key, "counts": result["counts"]})
    return json.dumps(result, indent=1)


@mcp.tool
def classify_flags(min_age_hours: float = 72.0, window_days: int = 60) -> str:
    """Run the full scan: inventory → trace → ordered verdict rules for every
    flag. Returns the report with verdicts REMOVABLE / STILL_LIVE / UNKNOWN /
    SKIP, each with path:line evidence. UNKNOWN flags require a human answer —
    ask the user via a question; do not guess."""
    report = pipeline.scan(min_age_hours=min_age_hours, window_days=window_days)
    return json.dumps(report, indent=1)


@mcp.tool
def plan_removal(flag_key: str) -> str:
    """Show the removal plan for one classified flag: verdict, evidence, tier
    (code-and-flag / tests-and-flag / flag-only) and what the patch would do."""
    entry = _flag_entry(flag_key)
    audit.log("agent", "plan_removal", {"flag": flag_key, "verdict": entry["verdict"]})
    return json.dumps(entry, indent=1)


@mcp.tool
def apply_and_test(flag_key: str) -> str:
    """Apply the removal patch on a disposable copy of the repo and run the
    test suite. Only a green test run may proceed to open_pr; a red run
    downgrades the flag to plan-only. This changes code — expect approval."""
    entry = _flag_entry(flag_key)
    if entry["verdict"] != "REMOVABLE":
        return json.dumps({"error": f"flag is {entry['verdict']}, not REMOVABLE"})
    tier = entry.get("removal_tier") or "flag-only"
    if tier == "flag-only":
        result = {"tier": tier, "patched": False, "tests_pass": True,
                  "detail": "no code references; flag-only delete is plan-only in Unleash"}
    else:
        work = _WORK_ROOT / flag_key
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        shutil.copytree(pipeline.default_repo(), work, dirs_exist_ok=True)
        result = remover.apply_and_test(str(work), flag_key,
                                        entry.get("default_enabled", False), tier)
        audit.log("agent", "apply_and_test",
                  {"flag": flag_key, "tests_pass": result.get("tests_pass")})
        if result.get("tests_pass"):
            # keep the work dir for open_pr
            result["work_dir"] = str(work)
    merged = {**entry, "apply_result": result}
    return json.dumps(merged, indent=1)


@mcp.tool
def open_pr(flag_key: str) -> str:
    """Open the cleanup pull request on GitHub for a flag whose removal passed
    tests. REQUIRES HUMAN APPROVAL — the harness will pause this tool call.
    The agent can never merge: the token has no merge scope, by design."""
    entry = _flag_entry(flag_key)
    if entry["verdict"] != "REMOVABLE":
        return json.dumps({"error": f"flag is {entry['verdict']}, not REMOVABLE"})
    if not prs.available():
        return json.dumps({
            "error": "GitHub not configured; set GITHUB_REPO, GITHUB_TOKEN, "
                     "REAPER_REPO_SOURCE to open real PRs",
        })
    work = _WORK_ROOT / flag_key
    diff = ""
    if (work / ".git").exists() or work.exists():
        diff = remover.git_diff(str(work))
    result = prs.open_pr(
        flag_key=flag_key,
        verdict=entry,
        diff=diff,
        test_output=entry.get("apply_result", {}).get("test_output", ""),
        last_seen_at=entry.get("last_seen_at"),
        last_hour_count=entry.get("last_hour_count", 0),
        plan_only=entry.get("removal_tier") in (None, "flag-only"),
    )
    return json.dumps(result, indent=1)


@mcp.tool
def get_report() -> str:
    """Get the current verdict report (all flags, evidence, summary counts)."""
    return json.dumps(pipeline.build_report(), indent=1)


@mcp.tool
def answer_unknown(flag_key: str, answer: str) -> str:
    """Record the human's answer for an UNKNOWN flag and reclassify it.
    answer must be 'in-use' (flag is live) or 'dead' (safe to remove).
    Only call this after the human has actually answered."""
    entry = pipeline.record_answer(flag_key, answer)
    return json.dumps(entry, indent=1)


# -------------------------------------------------------- backup views ----

@mcp.custom_route("/report", methods=["GET"])
async def _report(request):
    return JSONResponse(pipeline.build_report())


@mcp.custom_route("/audit", methods=["GET"])
async def _audit_view(request):
    return JSONResponse({"events": audit.tail(500)})


if __name__ == "__main__":
    port = int(os.environ.get("REAPER_MCP_PORT", "8900"))
    mcp.run(transport="http", host="127.0.0.1", port=port, path="/mcp")
