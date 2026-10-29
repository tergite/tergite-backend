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

import bcrypt
import pytest

from .utils.params import HAS_QUANTIFY

pytest_plugins = [
    "app.tests.utils.clients",
    "app.tests.utils.connections",
    "app.tests.utils.datetime",
    "app.tests.utils.io",
    "app.tests.utils.mss",
    "app.tests.utils.redis",
    "app.tests.utils.rq",
]


@pytest.fixture(autouse=True, scope="session")
def _run_in_the_test_root():
    """Runs the suite in this worker's own folder, as some libraries write files where
    they run"""
    project_root = Path.cwd()
    os.chdir(TEST_ROOT)
    yield
    os.chdir(project_root)


@pytest.fixture(autouse=True)
def _hash_passwords_cheaply(mocker):
    """Hashes at bcrypt's lowest cost: its default takes a quarter of a second a user"""
    lowest_cost_salt = bcrypt.gensalt(rounds=4)
    mocker.patch("bcrypt.gensalt", return_value=lowest_cost_salt)


@pytest.fixture(autouse=True)
def _clear_connection_caches():
    """Reset all module-level connection caches before every test."""
    from app.services.external.mss.service import disconnect_mss_client
    from app.services.scheduler.store import clear_jobs_stores_registry
    from app.services.scheduler.utils import clear_quantum_executor
    from app.utils.redis import clear_redis_connections
    from app.utils.sql_db import clear_sql_engine_cache

    clear_redis_connections(ignore_errors=True)
    clear_jobs_stores_registry()
    disconnect_mss_client(ignore_errors=True)
    clear_quantum_executor(ignore_errors=True)
    # Must also clear the SQL engine cache so that _clear_test_db's drop_all
    # forces a fresh create_all on the next test (cache hit skips create_all).
    clear_sql_engine_cache()
    yield
    clear_redis_connections(ignore_errors=True)
    clear_jobs_stores_registry()
    disconnect_mss_client(ignore_errors=True)
    clear_quantum_executor(ignore_errors=True)
    clear_sql_engine_cache()


@pytest.fixture(autouse=True, scope="session")
def _close_cached_instruments():
    """Closes the dummy clusters MockQuantifyExecutor keeps from test to test"""
    yield
    if HAS_QUANTIFY:
        from app.libs.quantum_executor.quantify.executor import QuantifyExecutor

        QuantifyExecutor._close_cached_instruments()


@pytest.fixture(autouse=True, scope="session")
def _configure_logging_for_tests():
    """Configure logging for tests"""
    is_debug = os.getenv("DEBUG", "").strip().lower() == "true"
    if not is_debug:
        # silence rq logs
        logging.getLogger("rq").setLevel(logging.WARNING)
        logging.getLogger("rq.worker").setLevel(logging.WARNING)
        logging.getLogger("rq.queue").setLevel(logging.WARNING)
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
