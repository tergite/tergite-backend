"""This is the entry point for the actual BCC app."""

import logging
import sys

import settings

# setup logger for the app
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    stream=sys.stdout,
)
