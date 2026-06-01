"""Command-line interface."""

from __future__ import annotations

import argparse
import sys

from .app import check_config, run_daemon, run_once
from .config import ConfigError
from .logging_config import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ptp-unregistered-cleaner")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run-once", help="Run one cleanup pass and exit")
    subparsers.add_parser("daemon", help="Run forever at the configured interval")
    subparsers.add_parser("check-config", help="Validate configuration and required env vars")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run-once":
            run_once()
        elif args.command == "daemon":
            run_daemon()
        elif args.command == "check-config":
            check_config()
        else:
            build_parser().error(f"unknown command: {args.command}")
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
