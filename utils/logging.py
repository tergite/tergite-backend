"""The application's error logger, and the set-up of its logging"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from filelock import FileLock

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


class SharedRotatingFileHandler(RotatingFileHandler):
    """Writes to a log file that other processes write too, rotating it into numbered
    backups once it would grow past a size; it rotates under a lock file beside the log, and
    follows the log to its new file once another process rotated it"""

    def __init__(self, filename: Path, max_bytes: int, backup_count: int):
        """
        Args:
            filename: the log file, opened at the first record
            max_bytes: the size past which the log is rotated
            backup_count: the number of backups kept, the oldest dropped
        """
        super().__init__(
            filename, maxBytes=max_bytes, backupCount=backup_count, delay=True
        )
        self._lock_file = FileLock(Path(filename).with_suffix(".lock"))

    def emit(self, record: logging.LogRecord) -> None:
        """Writes the record to the log, rotating it first if it would grow too big

        Args:
            record: the record to write
        """
        with self._lock_file:
            self._open_current_log()
            super().emit(record)

    def _open_current_log(self) -> None:
        """Reopens the log at its path if another process rotated it, else goes to its end,
        where the other processes have written"""
        if self.stream is None:
            return
        try:
            is_open = os.path.samestat(
                os.stat(self.baseFilename), os.fstat(self.stream.fileno())
            )
        except FileNotFoundError:
            is_open = False
        if is_open:
            self.stream.seek(0, os.SEEK_END)
        else:
            self.stream.close()
            # opened again as the record is written
            self.stream = None  # type: ignore[assignment]
