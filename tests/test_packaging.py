# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs AB 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""Tests of the wheel, which `uv tool install` installs bcc from"""

import configparser
import os
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[1]


@pytest.fixture(scope="module")
def wheel_site(tmp_path_factory) -> Path:
    """The wheel, built from this checkout and unpacked as an install would put it"""
    tmp_path = tmp_path_factory.mktemp("wheel")
    # through the sdist, as a wheel built in place takes in whatever a past build left
    # in build/lib
    subprocess.run(
        ["uv", "build", "--out-dir", str(tmp_path / "dist")],
        cwd=_ROOT,
        capture_output=True,
        check=True,
    )
    (wheel,) = (tmp_path / "dist").glob("*.whl")
    site = tmp_path / "site"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
    return site


def test_wheel_names_bcc_command(wheel_site):
    (entry_points_file,) = wheel_site.glob("*.dist-info/entry_points.txt")
    entry_points = configparser.ConfigParser()
    entry_points.read(entry_points_file)

    assert entry_points["console_scripts"]["bcc"] == "main:main"


def test_wheel_bcc_command_runs(wheel_site, tmp_path):
    result = _run_from_wheel(
        wheel_site,
        tmp_path,
        "import sys, main\n"
        "print(main.__file__)\n"
        "sys.argv = ['bcc', '--help']\n"
        "main.main()",
    )

    assert result.stdout.splitlines()[0] == str(wheel_site / "main.py")
    assert "Manage the Backend Control Computer" in result.stdout


def test_wheel_migrates_database(wheel_site, tmp_path):
    database = tmp_path / "bcc.db"

    _run_from_wheel(
        wheel_site,
        tmp_path,
        "from utils.sql_db import migrate_sql_db\n"
        f"migrate_sql_db('sqlite:///{database}')",
    )

    with sqlite3.connect(database) as connection:
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master")
        }
    assert {"job", "alembic_version"} <= tables


def _run_from_wheel(
    site: Path, cwd: Path, code: str
) -> subprocess.CompletedProcess[str]:
    """Runs the code in a fresh interpreter that imports the backend from the unpacked
    wheel rather than this checkout, in a folder outside it"""
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(site)},
        capture_output=True,
        text=True,
        check=True,
    )
