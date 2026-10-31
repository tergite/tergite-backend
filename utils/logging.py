"""The application's error logger, and the set-up of its logging"""

import logging
import sys

err_logger = logging.getLogger("uvicorn.error")


def set_up_logging(level: str) -> None:
    """Logs at the given level, e.g. ERROR, to stdout, unless the process logs somewhere already

    Args:
        level: the name of the log level; ERROR if it names none
    """
    logging.basicConfig(
        level=getattr(logging, level, logging.ERROR),
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
