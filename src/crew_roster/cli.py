"""Command line entry point.

    python -m crew_roster generate     build raw CSVs
    python -m crew_roster prepare      clean, transform and validate into SQLite
"""

from __future__ import annotations

import argparse
import logging
import sys

from crew_roster.config import load_config
from crew_roster.data.generate import generate
from crew_roster.data.pipeline import connect, run_pipeline
from crew_roster.data.validate import ValidationFailed, assert_valid, run_validation
from crew_roster.logging_setup import setup_logging

log = logging.getLogger("crew_roster")


def cmd_generate(config, _args) -> int:
    g = config.generation
    generate(config.paths.raw_dir, g.seed, g.profile, g.period_start, g.period_days, g.inject_dirty_rows)
    return 0


def cmd_prepare(config, _args) -> int:
    run_pipeline(config)
    with connect(config.paths.database) as conn:
        issues = run_validation(conn)
    try:
        assert_valid(issues)
    except ValidationFailed as exc:
        log.error("%s. Fix the input data before solving.", exc)
        return 2
    return 0


COMMANDS = {
    "generate": (cmd_generate, "Generate synthetic raw CSVs"),
    "prepare": (cmd_prepare, "Clean, transform and validate raw data into SQLite"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crew_roster", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="Path to a TOML config (default config/default.toml)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, (_fn, help_text) in COMMANDS.items():
        sub.add_parser(name, help=help_text)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    setup_logging(config.log_level, config.paths.output_dir / "logs" / "crew_roster.log")
    fn, _ = COMMANDS[args.command]
    return fn(config, args)


if __name__ == "__main__":
    sys.exit(main())
