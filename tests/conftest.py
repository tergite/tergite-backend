from .utils.env import (
    TEST_ROOT,
    setup_test_env,
)

# set up the environment before any other import
setup_test_env()

import logging
import os
import sys
from pathlib import Path

import pytest

import settings
from utils.config import create_device_folders
from utils.logging import err_logger

from .utils.params import HAS_QUANTIFY

create_device_folders(settings.get_device_settings())

pytest_plugins = [
    "tests.utils.clients",
    "tests.utils.connections",
    "tests.utils.datetime",
    "tests.utils.io",
    "tests.utils.mss",
    "tests.utils.systemd",
    "tests.utils.workers",
]


@pytest.fixture(autouse=True, scope="session")
def _run_in_the_test_root():
    """Runs the suite in this worker's own folder, as some libraries write files where
    they run"""
    project_root = Path.cwd()
    os.chdir(TEST_ROOT)
    yield
    os.chdir(project_root)


@pytest.fixture(autouse=True, scope="session")
def _close_cached_instruments():
    """Closes the dummy clusters MockQuantifyExecutor keeps from test to test"""
    yield
    if HAS_QUANTIFY:
        from libs.quantum_executor.quantify.executor import QuantifyExecutor

        QuantifyExecutor._close_cached_instruments()


@pytest.fixture(autouse=True, scope="session")
def _configure_logging_for_tests():
    """Configure logging for tests"""
    # so that the errors are seen in the terminal
    err_logger.error = print
    err_logger.info = print
    err_logger.warning = print
    err_logger.debug = print
    is_debug = os.getenv("DEBUG", "").strip().lower() == "true"
    if not is_debug:
        yield
        return

    if is_debug:
        root = logging.getLogger()
        # Remove any preconfigured handlers (libraries may have added them)
        for h in root.handlers[:]:
            root.removeHandler(h)
        # Use stderr (or sys.__stdout__) which we won't close
        h = logging.StreamHandler(sys.__stderr__)
        fmt = logging.Formatter("%(asctime)s [%(levelname)-8s] %(name)s: %(message)s")
        h.setFormatter(fmt)
        root.addHandler(h)
        root.setLevel(
            getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
        )
        yield
        # No need to close sys.__stderr__; just flush
        for h in root.handlers[:]:
            try:
                h.flush()
            except:
                pass
