"""Minimal WP-A CLI scaffold; full operational commands belong to WP-D."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    """Build the minimal top-level AtlasRAG CLI parser."""

    parser = argparse.ArgumentParser(
        prog="atlasrag",
        description="AtlasRAG local server and operational CLI.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        help="Operational command (implemented in Stage 11 WP-D).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Import-safe entrypoint scaffold until WP-D wires subcommands."""

    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command is None:
        parser.print_help()
    else:
        parser.error(
            f"command {args.command!r} is not implemented in WP-A (planned for WP-D)"
        )
    return 2
