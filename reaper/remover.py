"""Removal planning and patching.

Trivial literal pattern (AGENT.md §6): the flag key appears as a plain string
literal passed to the flag-check call used as a direct if-condition or ternary.
Branch survival rule: keep the branch matching the flag's current default
evaluation value (off → keep the else/default branch).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_CALL = r"(?:\w+\.)?is_enabled\(\s*[\"']{key}[\"']\s*(?:,[^)]*)?\)"


def _call_rx(key: str) -> re.Pattern:
    return re.compile(_CALL.format(key=re.escape(key)))


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def _block_extent(lines: list[str], start: int, indent: int) -> int:
    """Return index one past the last line of the block starting at `start`."""
    i = start + 1
    while i < len(lines):
        if lines[i].strip() == "":
            i += 1
            continue
        if _indent_of(lines[i]) <= indent:
            break
        i += 1
    return i


def _dedent_block(lines: list[str], lo: int, hi: int, drop: int) -> list[str]:
    out = []
    for line in lines[lo:hi]:
        if line.strip() == "":
            out.append(line)
        else:
            out.append(line[drop:] if len(line) >= drop and line[:drop].strip() == "" else line)
    return out


def _plan_if_else(lines: list[str], key: str, default_enabled: bool) -> list[dict] | None:
    """Find every `if is_enabled("K"): ... else: ...` and emit replacement ops."""
    rx = _call_rx(key)
    ops: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("if ") and rx.search(line) and " and " not in line and " or " not in line:
            indent = _indent_of(line)
            cond_end = i
            # condition may span multiple lines inside parens — only handle single-line
            if line.count("(") != line.count(")"):
                return None
            if_body_hi = _block_extent(lines, cond_end, indent)
            j = if_body_hi
            # skip blank lines
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            has_else = j < len(lines) and lines[j].strip() == "else:" and _indent_of(lines[j]) == indent
            if has_else:
                else_body_hi = _block_extent(lines, j, indent)
                keep = "if" if default_enabled else "else"
                ops.append({
                    "op": "replace_if_else",
                    "if_line": i, "else_line": j, "end": else_body_hi,
                    "indent": indent, "keep": keep,
                })
            elif default_enabled:
                # bare if + flag ON: unwrap — keep the body, dedented
                ops.append({
                    "op": "replace_if_else",
                    "if_line": i, "else_line": if_body_hi, "end": if_body_hi,
                    "indent": indent, "keep": "if",
                })
            else:
                # bare if + flag OFF: delete the whole block
                ops.append({
                    "op": "replace_if_else",
                    "if_line": i, "else_line": i, "end": if_body_hi,
                    "indent": indent, "keep": "none",
                })
            i = ops[-1]["end"]
            continue
        i += 1
    return ops or None


def _apply_if_else_ops(lines: list[str], ops: list[dict]) -> list[str]:
    # apply bottom-up so indices stay valid
    out = list(lines)
    for op in sorted(ops, key=lambda o: o["if_line"], reverse=True):
        drop = 4  # body sits one indent level deeper than the if-line
        if op["keep"] == "if":
            body = _dedent_block(out, op["if_line"] + 1, op["else_line"], drop)
        elif op["keep"] == "none":
            body = []
        else:
            body = _dedent_block(out, op["else_line"] + 1, op["end"], drop)
        out[op["if_line"]:op["end"]] = body
    return out


def _plan_test_blocks(lines: list[str], key: str) -> list[dict] | None:
    """Delete whole test functions that mention the key (any usage counts —
    assertions, overrides, fixtures)."""
    starts = [i for i, l in enumerate(lines) if re.match(r"^(def test_)", l)]
    ops = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        if any(key in l for l in lines[start:end]):
            # also swallow trailing blank lines
            while end < len(lines) and lines[end].strip() == "":
                end += 1
            ops.append({"op": "delete_block", "start": start, "end": end})
    return ops or None


def _apply_delete_ops(lines: list[str], ops: list[dict]) -> list[str]:
    out = list(lines)
    for op in sorted(ops, key=lambda o: o["start"], reverse=True):
        del out[op["start"]:op["end"]]
    return out


# ---------------------------------------------------------------------------


def plan_trivial_removal(key: str, default_enabled: bool, refs: list[dict],
                         repo: str = ".") -> dict | None:
    """Plan for rule 5 (code-and-flag). Returns ops per file, or None.

    Paths in refs are relative to the repo root (ripgrep runs with cwd=repo).
    """
    files: dict[str, list[dict]] = {}
    rx = _call_rx(key)
    for ref in refs:
        path = ref["path"]
        if path in files:
            continue
        try:
            text = (Path(repo) / path).read_text()
        except OSError:
            return None
        lines = text.splitlines(keepends=True)
        # a mixed file (if/else in one place, messy use elsewhere) is non-trivial
        ops = _plan_if_else(lines, key, default_enabled)
        if ops is None:
            return None
        covered = set()
        for op in ops:
            covered.update(range(op["if_line"], op["end"]))
        # every call site must be covered by a planned op
        for n, line in enumerate(lines):
            if rx.search(line) and n not in covered:
                return None
        files[path] = ops
    if not files:
        return None
    return {"files": [{"path": p, "ops": ops} for p, ops in files.items()]}


def plan_test_removal(key: str, test_refs: list[dict], repo: str = ".") -> dict | None:
    files: dict[str, list[dict]] = {}
    for ref in test_refs:
        path = ref["path"]
        if path in files:
            continue
        lines = (Path(repo) / path).read_text().splitlines(keepends=True)
        ops = _plan_test_blocks(lines, key)
        if ops is None:
            return None
        files[path] = ops
    return {"files": [{"path": p, "ops": ops} for p, ops in files.items()]}


# ---------------------------------------------------------------------------


def apply_ops(repo: str, plan: dict) -> list[str]:
    """Apply plan ops to the working tree; returns changed file paths."""
    changed = []
    for entry in plan["files"]:
        path = Path(repo) / entry["path"]
        lines = path.read_text().splitlines(keepends=True)
        if any(op["op"] == "replace_if_else" for op in entry["ops"]):
            lines = _apply_if_else_ops(lines, entry["ops"])
        else:
            lines = _apply_delete_ops(lines, entry["ops"])
        path.write_text("".join(lines))
        changed.append(entry["path"])
    return changed


def run_tests(repo: str, timeout: int = 300) -> tuple[bool, str]:
    proc = subprocess.run(
        ["python3", "-m", "pytest", "-q"],
        cwd=repo, capture_output=True, text=True, timeout=timeout,
    )
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, output[-4000:]


def git_diff(repo: str) -> str:
    proc = subprocess.run(["git", "diff"], cwd=repo, capture_output=True, text=True)
    return proc.stdout


def apply_and_test(repo: str, key: str, default_enabled: bool, tier: str) -> dict:
    """Full tier-1 flow on a disposable checkout: plan → patch → test → diff."""
    from . import tracer

    trace = tracer.trace(repo, key)
    refs = trace["references"]
    if tier == "tests-and-flag":
        plan = plan_test_removal(key, [r for r in refs if r["kind"] == "test"], repo=repo)
    elif tier == "code-and-flag":
        code_plan = plan_trivial_removal(key, default_enabled,
                                         [r for r in refs if r["kind"] == "literal"], repo=repo)
        # the removal PR must also drop tests of the removed behavior
        test_plan = plan_test_removal(key, [r for r in refs if r["kind"] == "test"], repo=repo)
        if code_plan is None:
            plan = None
        elif test_plan is None:
            plan = code_plan
        else:
            plan = {"files": code_plan["files"] + test_plan["files"]}
    else:
        return {"tier": tier, "patched": False, "tests_pass": False,
                "detail": f"tier '{tier}' is plan-only (no code patch)"}

    if plan is None:
        return {"tier": tier, "patched": False, "tests_pass": False,
                "detail": "no trivial removal plan could be built; downgrade to plan-only"}

    apply_ops(repo, plan)
    ok, output = run_tests(repo)
    diff = git_diff(repo)
    return {
        "tier": tier,
        "patched": True,
        "tests_pass": ok,
        "test_output": output,
        "diff": diff,
        "files": [f["path"] for f in plan["files"]],
    }
