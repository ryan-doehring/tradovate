from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tradovate_bot.jobs import run_eod_flat, run_news_guard, run_sync


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradovate-bot")
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=Path("artifacts"),
        help="Directory for JSON run reports",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sync_parser = sub.add_parser("sync", help="Reconcile Google Sheet with Tradovate orders")
    sync_parser.add_argument("--dry-run", action="store_true")

    news_parser = sub.add_parser("news-guard", help="News blackout flatten and reopen")
    news_parser.add_argument("--dry-run", action="store_true")

    eod_parser = sub.add_parser("eod-flat", help="Close open positions before session close")
    eod_parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "sync":
        return run_sync(dry_run=args.dry_run, artifact_dir=args.artifact_dir)
    if args.command == "news-guard":
        return run_news_guard(dry_run=args.dry_run, artifact_dir=args.artifact_dir)
    if args.command == "eod-flat":
        return run_eod_flat(dry_run=args.dry_run, artifact_dir=args.artifact_dir)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
