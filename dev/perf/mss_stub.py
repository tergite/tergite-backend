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
"""An MSS that accepts every device event the BCC sends it, at once, so that the performance
baselines measure the BCC alone; dev/perf/record.sh runs it"""

import argparse
import json

from websockets.sync.server import ServerConnection, serve


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    with serve(accept_events, "127.0.0.1", args.port) as server:
        server.serve_forever()


def accept_events(connection: ServerConnection) -> None:
    """Answers each event as MSS does when it accepts it"""
    for message in connection:
        event = json.loads(message)
        connection.send(
            json.dumps({"status": "success", "id": event["id"], "data": event["data"]})
        )


if __name__ == "__main__":
    main()
