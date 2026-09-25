"""CLI: scan the demo repo + Unleash and print the verdict table.

Usage: python -m reaper.scan [--min-age-hours 0] [--window-days 60] [--repo PATH]
"""
from __future__ import annotations

import argparse

from . import pipeline

COLORS = {"REMOVABLE": "\033[92m", "STILL_LIVE": "\033[2m", "UNKNOWN": "\033[93m", "SKIP": "\033[2m"}
RESET = "\033[0m"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None)
    ap.add_argument("--min-age-hours", type=float, default=72.0,
                    help="flags younger than this are skipped (0 disables the age guard)")
    ap.add_argument("--window-days", type=int, default=60)
    args = ap.parse_args()

    report = pipeline.scan(repo=args.repo, min_age_hours=args.min_age_hours,
                           window_days=args.window_days)
    print(f"\nFlag Reaper scan — {report['scanned_at']}\n")
    print(f"{'VERDICT':<12} {'FLAG':<24} {'REASON':<22} DETAIL")
    print("-" * 100)
    for entry in report["flags"]:
        color = COLORS.get(entry["verdict"], "")
        print(f"{color}{entry['verdict']:<12}{RESET} {entry['flag']:<24} "
              f"{entry['reason']:<22} {entry['detail'][:48]}")
    print("-" * 100)
    print("summary:", report["summary"])
    for entry in report["flags"]:
        if entry["verdict"] == "UNKNOWN":
            print(f"\nGATE — {entry['flag']}: {entry.get('question', entry['detail'])}")


if __name__ == "__main__":
    main()
