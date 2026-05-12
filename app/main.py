"""
CLI entrypoint for the Revenue Ops Copilot.

Usage:
    python -m app.main examples/leads.csv
    python -m app.main --help
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from app.workflows.workflow import RevenueOpsWorkflow

# Load .env from project root (two levels up from this file)
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)
else:
    load_dotenv()  # fallback to default discovery


def _setup_logging(verbose: bool = False) -> None:
    """Configure structured logging with timestamps."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s | %(name)-10s | %(levelname)-5s | %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="revenue-ops-copilot",
        description="Revenue Ops Copilot - review leads, classify risk/opportunity, "
        "prioritise actions, and generate operator summaries.",
    )
    parser.add_argument(
        "csv_path",
        type=str,
        help="Path to a CSV file containing lead records.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug-level logging.",
    )
    return parser


def main() -> None:
    """Parse args and run the workflow."""
    parser = _build_parser()
    args = parser.parse_args()

    _setup_logging(verbose=args.verbose)
    logger = logging.getLogger("cli")

    csv_path = Path(args.csv_path)
    if not csv_path.exists():
        logger.error("File not found: %s", csv_path)
        sys.exit(1)

    state = RevenueOpsWorkflow.run_sync(csv_path)

    if not state.metrics.success:
        logger.warning(
            "Workflow completed with issues - see outputs/summary.md for details."
        )
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
