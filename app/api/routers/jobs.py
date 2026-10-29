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
import copy
from typing import Optional

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy import Engine
from typing_extensions import Annotated

from app.services import scheduler
from app.services.booking.models import MSSTokenClaims

from ...libs.queues.dtos import Job, JobStatus, QueueContext
from ...services.scheduler.queues import QueuePool, get_queue_pool
from ...utils.api import CancellationDetails, GeneralMessage, PaginatedListResponse
from ..dependencies import (
    MSSAuthDetails,
    get_backend_name,
    get_booking_db,
    get_mss_token_claims_dep,
    get_queue_context_if_online,
    get_unverified_mss_is_admin,
    get_verified_mss_details,
    get_verified_mss_user_id,
    validate_job_file,
)

router = APIRouter()


@router.post("/jobs")
async def submit_job(
    context: QueueContext = Depends(get_queue_context_if_online),
    upload_file: Annotated[UploadFile, Depends(validate_job_file)] = File(...),
    token_claims: MSSTokenClaims = Depends(get_mss_token_claims_dep(job_exists=False)),
    queue_pool: QueuePool = Depends(get_queue_pool),
    backend_name: str = Depends(get_backend_name),
    booking_db_engine: Engine = Depends(get_booking_db),
    force_normal_queue: bool = Query(default=False),
) -> Job:
    """Receives quantum jobs to process. This can be done by any IP address

    Args:
        upload_file: the quantum job file uploaded
        token_claims: the user_id and job_id associated with this request
        queue_pool: the collection of queues to run the jobs on
        context: the queue context for the job in the queue
        backend_name: the backend to use for the job
        booking_db_engine: the SQL database engine to query
        force_normal_queue: whether to force the job to run on the normal queue or not

    Returns:
        the submitted job
    """
    context = copy.deepcopy(context)
    context["force_normal_queue"] = force_normal_queue

    return scheduler.submit_job_file(
        context,
        queues=queue_pool,
        upload_file=upload_file,
        credentials=token_claims,
        backend_name=backend_name,
        db_engine=booking_db_engine,
    )


@router.get("/jobs/{job_id}")
async def view_job(
    job_id: str,
    user_id: str = Depends(get_verified_mss_user_id),
    is_mss_admin: bool = Depends(get_unverified_mss_is_admin),
    context: QueueContext = Depends(get_queue_context_if_online),
    db_engine: Engine = Depends(get_booking_db),
) -> Job:
    """View the job of given job_id if job belongs to current user or if user is admin

    Args:
        context: the queue context for the job in the queue
        job_id: the unique identifier of the job
        user_id: the user_id as provided by MSS
        is_mss_admin: whether the user is an mss admin or not
        db_engine: the SQL database engine to query

    Returns:
        the job of the given job_id
    """
    return scheduler.get_job(
        context,
        job_id=job_id,
        user_id=user_id,
        is_mss_admin=is_mss_admin,
        db_engine=db_engine,
    )


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    details: CancellationDetails,
    context: QueueContext = Depends(get_queue_context_if_online),
    mss_auth_details: MSSAuthDetails = Depends(get_verified_mss_details),
    queue_pool: QueuePool = Depends(get_queue_pool),
    db_engine: Engine = Depends(get_booking_db),
) -> GeneralMessage:
    """Cancels the job of given job_id if job belongs to current user or if user is admin

    Args:
        job_id: the unique identifier of the job
        details: the extra information passed when canceling the job
        context: the queue context for the job in the queue
        mss_auth_details: the auth details from MSS for this request
        queue_pool: the collection of queues to run the jobs on
        db_engine: the SQL database engine to query

    Returns:
        a general message showing status
    Raises:
        NotAuthenticatedError: user not found
        ItemNotFoundError: Job {job_id} not found
        JobAlreadyCompleteError (406): job {job_id} is already {status}
    """
    scheduler.cancel_job(
        context,
        queues=queue_pool,
        job_id=job_id,
        user_id=mss_auth_details.user_id,
        is_mss_admin=mss_auth_details.is_mss_admin,
        reason=details.reason,
        db_engine=db_engine,
    )
    return {"status": "success", "detail": f"Job of id {job_id} cancelled"}


@router.delete("/jobs/{job_id}")
async def remove_job(
    job_id: str,
    context: QueueContext = Depends(get_queue_context_if_online),
    user_id: str = Depends(get_verified_mss_user_id),
    queue_pool: QueuePool = Depends(get_queue_pool),
    is_mss_admin: bool = Depends(get_unverified_mss_is_admin),
    db_engine: Engine = Depends(get_booking_db),
) -> GeneralMessage:
    """Deletes the job of given job_id if job belongs to current user or if user is admin

    Args:
        job_id: the unique identifier of the job
        context: the queue context for the job in the queue
        user_id: the JWT token for the user, transformed into user_id by callback
        queue_pool: the collection of queues to run the jobs on
        is_mss_admin: whether the user is an mss admin or not
        db_engine: the SQL database engine to query

    Returns:
        a general message showing status
    Raises:
        NotAuthenticatedError: user not found
        ItemNotFoundError: Job {job_id} not found
    """
    scheduler.delete_job(
        context,
        queues=queue_pool,
        job_id=job_id,
        user_id=user_id,
        is_mss_admin=is_mss_admin,
        db_engine=db_engine,
    )
    return {"status": "success", "detail": f"Job of id {job_id} deleted"}


@router.get("/jobs")
async def view_jobs(
    context: QueueContext = Depends(get_queue_context_if_online),
    user_id: str = Depends(get_verified_mss_user_id),
    status: Optional[JobStatus] = Query(default=None),
    skip: int = Query(default=0),
    limit: Optional[int] = Query(default=None),
) -> dict:
    """Views all jobs that belong to the current user

    Args:
        context: the queue context for the jobs in the queue
        user_id: the unique identifier of the currently logged in user
        status: the status of the jobs to return; default = None, i.e. all statuses
        skip: number of records to ignore at the top of the returned results; default is 0
        limit: maximum number of records to return; default is None.

    Returns:
        the paginated list of the available bookings
    """
    data = scheduler.get_many_jobs(
        context, user_id=user_id, status=status, skip=skip, limit=limit
    )
    results = PaginatedListResponse[Job](skip=skip, limit=limit, data=data)
    return results.model_dump(mode="json")
