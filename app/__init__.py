"""This is the entry point for the actual BCC app."""

# setup logger for the app

import logging, os, sys

level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, level, logging.INFO),
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    stream=sys.stdout,
)