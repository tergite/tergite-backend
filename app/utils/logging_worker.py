import logging
import os
import sys

from rq import Worker


class LoggingWorker(Worker):
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
