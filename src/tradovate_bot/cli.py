from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tradovate_bot.jobs import run_news_guard, run_pre_close, run_sync, run_tick

HANDLERS = {
    "tick": (run_tick, "One cycle: guards first, then reconcile the sheet"),
    "sync": (run_sync, "Manual alias for tick (trade-sync report name)"),
    "news-guard": (run_news_guard, "Manual alias for tick (news-guard report name)"),
    "pre-close": (run_pre_close, "Manual alias for tick (pre-close report name)"),
    "eod-flat": (run_pre_close, "Deprecated alias for pre-close"),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradovate-bot")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, (_, help_text) in HANDLERS.items():
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--dry-run", action="store_true")
        command.add_argument(
            "--artifact-dir",
            type=Path,
            default=Path("artifacts"),
            help="Directory for JSON run reports",
        )

    args = parser.parse_args(argv)
    handler = HANDLERS[args.command][0]
    return handler(dry_run=args.dry_run, artifact_dir=args.artifact_dir)


if __name__ == "__main__":
    sys.exit(main())