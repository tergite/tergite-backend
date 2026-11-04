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
"""Writes the REST API's OpenAPI schema to openapi.json beside this file, without a server

Run from the repository root: `python -m dev.openapi.export`. It builds the schema with the
test settings, as tergite-frontend's does, and the SDK generates its models of the backend
from the file it writes.
"""

import json
from pathlib import Path

from tests.utils.env import setup_test_env

SCHEMA_FILE = Path(__file__).parent / "openapi.json"


def main() -> None:
    setup_test_env()
    from api import app  # settings read the environment set above on import

    SCHEMA_FILE.write_text(render_schema(app.openapi()))


def render_schema(schema: dict) -> str:
    """Renders the schema as the committed file holds it

    Args:
        schema: the app's OpenAPI schema

    Returns:
        the schema as sorted, indented JSON
    """
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    main()
