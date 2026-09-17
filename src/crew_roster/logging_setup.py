"""One place to configure logging for the CLI and scripts."""

from __future__ import annotations

import logging
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=level.upper(), format=FORMAT, handlers=handlers, force=True)
    # PuLP is chatty at INFO about temp files; keep it quiet.
    logging.getLogger("pulp").setLevel(logging.WARNING)
