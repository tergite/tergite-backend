# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""Routes for the jobs' log files"""
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from utils.exc import ItemNotFoundError

from ..dependencies import DeviceResourcesDep, get_mss_token_claims_dep

router = APIRouter()


@router.get(
    "/logfiles/{logfile_id}",
    dependencies=[Depends(get_mss_token_claims_dep(job_id_field="logfile_id"))],
)
async def download_logfile(device: DeviceResourcesDep, logfile_id: UUID):
    """Downloads the job logfile

    Args:
        device: what the device runs on
        logfile_id: the id of the logfile usually the job id

    Raises:
        ItemNotFoundError: logfile {logfile_id} not found
    """
    file = (device.settings.log_file_pool / str(logfile_id)).with_suffix(".hdf5")
    if file.exists():
        return FileResponse(file)
    raise ItemNotFoundError(f"logfile {logfile_id} not found")
