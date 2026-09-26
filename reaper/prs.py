"""GitHub PR creation via PyGithub. The PAT deliberately has NO merge scope —
the reaper can open PRs but can never merge them (hard constraint §12.2)."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from . import audit

REPO_SLUG = os.environ.get("GITHUB_REPO", "")          # e.g. user/flag-reaper-demo
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
SOURCE_REPO = os.environ.get("REAPER_REPO_SOURCE", "")  # https URL of the demo repo


def available() -> bool:
    return bool(REPO_SLUG and GITHUB_TOKEN and SOURCE_REPO)


def _run_git(*args: str, cwd: str) -> str:
    proc = subprocess.run(("git", *args), cwd=cwd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def open_pr(flag_key: str, verdict: dict, diff: str, test_output: str,
            last_seen_at: str | None, last_hour_count: int, plan_only: bool = False) -> dict:
    """Branch → apply diff → commit → push → PR with evidence + rollback note."""
    if not available():
        raise RuntimeError("set GITHUB_REPO, GITHUB_TOKEN and REAPER_REPO_SOURCE to open PRs")

    from github import Github, Auth

    work = tempfile.mkdtemp(prefix="reaper-pr-")
    authed_url = SOURCE_REPO.replace("https://", f"https://x-access-token:{GITHUB_TOKEN}@")
    _run_git("clone", "--depth", "1", authed_url, ".", cwd=work)
    branch = f"reaper/remove-{flag_key}"
    _run_git("checkout", "-b", branch, cwd=work)

    if not plan_only:
        patch_file = Path(work) / ".reaper.patch"
        patch_file.write_text(diff)
        _run_git("apply", ".reaper.patch", cwd=work)
        patch_file.unlink()

    _run_git("add", "-A", cwd=work)
    _run_git("-c", "user.name=flag-reaper", "-c", "user.email=reaper@local",
             "commit", "-m", f"Remove feature flag: {flag_key}", cwd=work)
    _run_git("push", "-q", authed_url, f"HEAD:refs/heads/{branch}", cwd=work)

    gh = Github(auth=Auth.Token(GITHUB_TOKEN))
    repo = gh.get_repo(REPO_SLUG)
    body = _pr_body(flag_key, verdict, test_output, last_seen_at, last_hour_count, plan_only)
    pr = repo.create_pull(title=f"reaper: remove feature flag `{flag_key}`",
                          body=body, head=branch, base=repo.default_branch)
    result = {"pr_url": pr.html_url, "branch": branch, "number": pr.number}
    audit.log("agent", "pr_opened", {"flag": flag_key, **result, "plan_only": plan_only})
    return result


def _pr_body(flag_key: str, verdict: dict, test_output: str,
             last_seen_at: str | None, last_hour_count: int, plan_only: bool) -> str:
    tier = verdict.get("removal_tier") or "plan-only"
    lines = [
        f"## Removal of `{flag_key}`",
        "",
        f"- **Verdict:** `{verdict['verdict']}` ({verdict['reason']})",
        f"- **Tier:** {tier}" + (" (plan-only — apply the plan below)" if plan_only else ""),
        f"- **Default evaluation value:** {'on' if verdict.get('default_enabled') else 'off'} "
        "→ the branch matching the default was kept",
        f"- **Usage:** lastSeenAt={last_seen_at or 'never'}, last-hour evaluations={last_hour_count}",
        "",
        "### Evidence",
        "",
        "```",
    ]
    for ev in verdict.get("evidence", [])[:8]:
        lines.append(f"{ev['path']}:{ev['line']}  [{ev['kind']}]  {ev['text'][:120]}")
    if not verdict.get("evidence"):
        lines.append("(no code references found)")
    lines += ["```", ""]
    if plan_only:
        lines += [
            "### Removal plan",
            "",
            f"1. Archive and delete `{flag_key}` in Unleash (Admin API: ",
            f"   `DELETE /api/admin/projects/default/features/{flag_key}`)",
            "2. Confirm no client applications report metrics for it afterwards",
            "",
        ]
    if test_output:
        lines += ["### Test results", "", "```", test_output.strip()[-800:], "```", ""]
    lines += [
        "---",
        f"**Rollback:** `git revert <merge-commit-sha>` — a single revert restores "
        f"the flag check and the removed branch.",
        "",
        "*Opened by the Feature Flag Reaper. This agent cannot merge: its token "
        "has no merge scope, by design.*",
    ]
    return "\n".join(lines)
