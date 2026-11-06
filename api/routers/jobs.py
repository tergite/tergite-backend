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
"""Routes for the jobs: their upload, status, cancellation and removal"""

from typing import Optional

from fastapi import APIRouter, Depends, File, Query, UploadFile
from typing_extensions import Annotated

from libs.queues.dtos import Job, JobStatus
from services import scheduler
from services.booking import MSSTokenClaims
from utils.messages import GeneralMessage

from ..dependencies import (
    DeviceResourcesDep,
    MSSAuthDetails,
    check_device_is_online,
    get_mss_token_claims_dep,
    get_unverified_mss_is_admin,
    get_verified_mss_details,
    get_verified_mss_user_id,
    validate_job_file,
)
from ..dtos import CancellationDetails, InvalidCircuitResponse

# a page of jobs by default, and the most in one, so that no list reads a backlog whole
JOBS_PAGE_SIZE = 100
MAX_JOBS_PAGE_SIZE = 1000

router = APIRouter()


@router.post(
    "/jobs",
    dependencies=[Depends(check_device_is_online)],
    responses={
        422: {
            "model": InvalidCircuitResponse,
            "description": "A circuit the device cannot run, as one that is not valid "
            "OpenQASM 3",
        }
    },
    # the uploaded file is a JobFile's JSON, which the spec's components hold
    openapi_extra={
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "encoding": {"upload_file": {"contentType": "application/json"}}
                }
            }
        }
    },
)
async def submit_job(
    device: DeviceResourcesDep,
    upload_file: Annotated[UploadFile, Depends(validate_job_file)] = File(...),
    token_claims: MSSTokenClaims = Depends(
        get_mss_token_claims_dep(should_job_exist=False)
    ),
    should_force_normal_queue: bool = Query(default=False, alias="force_normal_queue"),
) -> Job:
    """Receives quantum jobs to process. This can be done by any IP address

    Args:
        device: what the device runs on
        upload_file: the quantum job file uploaded
        token_claims: the user_id and job_id associated with this request
        should_force_normal_queue: whether to force the job to run on the normal queue or not

    Returns:
        the submitted job
    """
    return scheduler.submit_job_file(
        device,
        upload_file=upload_file,
        credentials=token_claims,
        should_force_normal_queue=should_force_normal_queue,
    )


@router.get("/jobs/{job_id}", dependencies=[Depends(check_device_is_online)])
async def view_job(
    device: DeviceResourcesDep,
    job_id: str,
    user_id: str = Depends(get_verified_mss_user_id),
    is_mss_admin: bool = Depends(get_unverified_mss_is_admin),
) -> Job:
    """View the job of given job_id if job belongs to current user or if user is admin

    Args:
        device: what the device runs on
        job_id: the unique identifier of the job
        user_id: the user_id as provided by MSS
        is_mss_admin: whether the user is an mss admin or not

    Returns:
        the job of the given job_id
    """
    return scheduler.get_job(
        device, job_id=job_id, user_id=user_id, is_mss_admin=is_mss_admin
    )


@router.post("/jobs/{job_id}/cancel", dependencies=[Depends(check_device_is_online)])
async def cancel_job(
    device: DeviceResourcesDep,
    job_id: str,
    details: CancellationDetails,
    mss_auth_details: MSSAuthDetails = Depends(get_verified_mss_details),
) -> GeneralMessage:
    """Cancels the job of given job_id if job belongs to current user or if user is admin

    Args:
        device: what the device runs on
        job_id: the unique identifier of the job
        details: the extra information passed when canceling the job
        mss_auth_details: the auth details from MSS for this request

    Returns:
        a general message showing status
    Raises:
        NotAuthenticatedError: user not found
        ItemNotFoundError: Job {job_id} not found
        JobAlreadyCompleteError (406): job {job_id} is already {status}
    """
    scheduler.cancel_job(
        device,
        job_id=job_id,
        user_id=mss_auth_details.user_id,
        is_mss_admin=mss_auth_details.is_mss_admin,
        reason=details.reason,
    )
    return {"status": "success", "detail": f"Job of id {job_id} cancelled"}


@router.delete("/jobs/{job_id}", dependencies=[Depends(check_device_is_online)])
async def remove_job(
    device: DeviceResourcesDep,
    job_id: str,
    user_id: str = Depends(get_verified_mss_user_id),
    is_mss_admin: bool = Depends(get_unverified_mss_is_admin),
) -> GeneralMessage:
    """Deletes the job of given job_id if job belongs to current user or if user is admin

    Args:
        device: what the device runs on
        job_id: the unique identifier of the job
        user_id: the JWT token for the user, transformed into user_id by callback
        is_mss_admin: whether the user is an mss admin or not

    Returns:
        a general message showing status
    Raises:
        NotAuthenticatedError: user not found
        ItemNotFoundError: Job {job_id} not found
    """
    scheduler.delete_job(
        device, job_id=job_id, user_id=user_id, is_mss_admin=is_mss_admin
    )
    return {"status": "success", "detail": f"Job of id {job_id} deleted"}


@router.get("/jobs", dependencies=[Depends(check_device_is_online)])
async def view_jobs(
    device: DeviceResourcesDep,
    user_id: str = Depends(get_verified_mss_user_id),
    status: Optional[JobStatus] = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=JOBS_PAGE_SIZE, ge=1, le=MAX_JOBS_PAGE_SIZE),
) -> dict:
    """Views all jobs that belong to the current user

    Args:
        device: what the device runs on
        user_id: the unique identifier of the currently logged in user
        status: the status of the jobs to return; default = None, i.e. all statuses
        skip: number of records to ignore at the top of the returned results; default is 0
        limit: maximum number of records to return; default is JOBS_PAGE_SIZE, at most
            MAX_JOBS_PAGE_SIZE

    Returns:
        the paginated list of the user's jobs
    """
    stored_jobs = scheduler.get_many_jobs(
        device, user_id=user_id, status=status, skip=skip, limit=limit
    )
    # each job without its fields that are None, as /jobs has always listed them
    jobs = [job.model_dump(mode="json", exclude_none=True) for job in stored_jobs]
    return {"skip": skip, "limit": limit, "data": jobs}
