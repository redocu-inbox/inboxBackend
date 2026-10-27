"""Configure logging for our own `app.*` loggers.

Named `logging_config` so it does not shadow Python's standard `logging` module.
Uvicorn keeps its own log format; we only attach a handler to the `app` logger.
"""

import logging
import sys


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )

    app_logger = logging.getLogger("app")
    app_logger.handlers.clear()
    app_logger.addHandler(handler)
    app_logger.setLevel(level.upper())
    # Don't also pass these records to the root logger (that would double-print).
    app_logger.propagate = False
