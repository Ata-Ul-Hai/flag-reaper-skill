#!/usr/bin/env python3
"""Sandbox script: apply a flag-removal patch and run the test suite.

Run inside the TrueForge sandbox after cloning the demo repo:

    python scripts/apply_and_test.py <flag-key> --repo ./checkout-service

Standalone (stdlib + subprocess only). Prints a JSON result:
    {"tests_pass": bool, "diff": str, ...}
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper import remover, tracer  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("flag_key")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--tier", default=None,
                    help="code-and-flag | tests-and-flag (default: auto)")
    ap.add_argument("--default-on", action="store_true",
                    help="flag's current default evaluation is ON (keep if-branch)")
    args = ap.parse_args()

    trace = tracer.trace(args.repo, args.flag_key)
    refs = trace["references"]
    literal = [r for r in refs if r["kind"] == "literal"]
    test = [r for r in refs if r["kind"] == "test"]

    if args.tier:
        tier = args.tier
    elif literal:
        tier = "code-and-flag"
    elif test:
        tier = "tests-and-flag"
    else:
        print(json.dumps({"tests_pass": False, "detail": "no references; flag-only (plan-only in Unleash)"}))
        return

    result = remover.apply_and_test(args.repo, args.flag_key, args.default_on, tier)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
