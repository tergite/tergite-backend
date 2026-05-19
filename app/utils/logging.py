import logging
import os
import sys

from rq import Worker

import settings

# error logger
err_logger = logging.getLogger("uvicorn.error")
# work around for testing to allow errors to be seen in terminal
if settings.APP_SETTINGS == "test":
    err_logger.error = print
    err_logger.info = print
    err_logger.warning = print
    err_logger.debug = print


class LoggingRqWorker(Worker):
    """A special RQ worker that logs its messages for ease of tracking"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        level_name = os.getenv("LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)

        root = logging.getLogger()
        # avoid duplicate handlers if RQ restarts a child, etc.
        if not root.handlers:
            h = logging.StreamHandler(sys.stdout)
            fmt = logging.Formatter(
                "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
            )
            h.setFormatter(fmt)
            root.addHandler(h)
        root.setLevel(level)
