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
"""Where the executor keeps a job's files, apart from the executor, which the API need not
load to find them"""

from pathlib import Path


def get_expt_metadata_file(job_id: str, folder: Path) -> Path:
    """Gets the path of the file in the folder that the job's experiments' metadata is
    pickled to, from its preprocessing to its execution

    Args:
        job_id: the ID of the job
        folder: the folder of the device's preprocessed jobs

    Returns:
        the path of the file
    """
    return folder / f"{job_id}-expt-metadata.pickle"
