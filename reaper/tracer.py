"""Reference tracing: ripgrep over a repo, references classified by kind."""
from __future__ import annotations

import os
import re
import shutil
import subprocess

from . import dynamic_detect

CONFIG_EXTS = dynamic_detect.CONFIG_EXTS

EXCLUDE_GLOBS = [
    "--glob", "!.git/",
    "--glob", "!.venv/",
    "--glob", "!node_modules/",
    "--glob", "!*.lock",
    "--glob", "!*.min.js",
]


_RG_CANDIDATES = (
    "/opt/homebrew/bin/rg",   # apple silicon brew
    "/usr/local/bin/rg",      # intel brew
    "/usr/bin/rg",
)


def _rg_path() -> str:
    """Locate ripgrep robustly: env override → PATH → known install paths.

    The MCP server process may run with a minimal PATH (launched headless by
    an agent harness), so PATH-only lookup is not enough for demo reliability.
    """
    override = os.environ.get("REAPER_RG_PATH")
    if override and os.path.exists(override):
        return override
    found = shutil.which("rg")
    if found:
        return found
    for candidate in _RG_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    raise RuntimeError("ripgrep (rg) is required: brew install ripgrep "
                       "(or set REAPER_RG_PATH)")


def _ripgrep(repo: str, pattern: str, fixed: bool = True, word: bool = True) -> list[dict]:
    cmd = [_rg_path(), "-n", "--no-heading", "-S", *EXCLUDE_GLOBS]
    if fixed:
        cmd.append("-F")
    if word:
        cmd.append("-w")
    cmd += [pattern, "."]
    proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    if proc.returncode not in (0, 1):  # 1 = no matches
        raise RuntimeError(f"ripgrep failed: {proc.stderr.strip()}")
    refs = []
    for line in proc.stdout.splitlines():
        path, lineno, text = line.split(":", 2)
        refs.append({"path": path.removeprefix("./"), "line": int(lineno), "text": text.strip()})
    return refs


def _kind(path: str, text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("#") or stripped.startswith("//"):
        return "comment"
    if re.search(r"(^|/)(tests?/|[^/]*_test\.py|test_[^/]*\.py)", path):
        return "test"
    if path.endswith(tuple(CONFIG_EXTS)):
        return "config-map"
    return "literal"


def trace(repo: str, flag_key: str) -> dict:
    """Exact references by kind, plus dynamic-construction candidates."""
    exact = _ripgrep(repo, flag_key)
    for ref in exact:
        ref["kind"] = _kind(ref["path"], ref["text"])
    # comments mention flags all the time; keep as evidence but they never
    # block removal (non-executing lines)
    blocking = [r for r in exact if r["kind"] != "comment"]

    dynamic = []
    if not blocking or all(r["kind"] == "config-map" for r in blocking):
        # key never appears at a call site — look for dynamic construction.
        # fragments run most-specific first; stop at the first fragment that
        # produces evidence (less specific fragments are noise magnets).
        for frag in dynamic_detect.fragments(flag_key):
            hits = []
            for ref in _ripgrep(repo, frag, fixed=False, word=False):
                hit = dynamic_detect.detect_dynamic(flag_key, ref["path"], ref["text"])
                if hit:
                    hits.append({**ref, **hit, "kind": "dynamic"})
            if hits:
                dynamic = hits
                break
        # de-duplicate by path:line
        seen = set()
        dynamic = [d for d in dynamic if not (d["path"], d["line"]) in seen and not seen.add((d["path"], d["line"]))]

    return {
        "flag_key": flag_key,
        "references": exact,
        "dynamic_candidates": dynamic,
        "counts": {
            "literal": sum(1 for r in blocking if r["kind"] == "literal"),
            "test": sum(1 for r in blocking if r["kind"] == "test"),
            "config_map": sum(1 for r in blocking if r["kind"] == "config-map"),
            "comment": sum(1 for r in exact if r["kind"] == "comment"),
            "dynamic": len(dynamic),
        },
    }
