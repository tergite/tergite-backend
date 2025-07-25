# This code is part of Tergite
#
# (C) Chalmers Next Labs 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
"""Shared Data Transfer Objects"""
import json
from datetime import datetime
from enum import Enum, unique
from functools import cached_property
from typing import (
    Any,
    Dict,
    List,
    Literal,
    NotRequired,
    Optional,
    Tuple,
    TypeAlias,
    TypedDict,
    Union,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic_core import CoreSchema, core_schema
from pydantic_core.core_schema import SerializationInfo
from qiskit.qobj import PulseQobj

from ...libs.qiskit_providers.utils.json_encoder import IQXJsonEncoder
from ..datetime import get_utc_now, to_utc, utc_now_str
from ..exc import JobAlreadyCompleteError
from ..model import PartialMeta
from ..redis_store import Schema
from ..strings import uuid_str

_STORAGE_ID_SEPARATOR = ":::"

HexMemory: TypeAlias = List[List[str]]
# IQ point = tuple(re, im) or list[re, im]  →  use Sequence[float] for both.
IQPoint: TypeAlias = Tuple[float, float] | List[float]
IQMemory: TypeAlias = List[List[List[IQPoint]]]
Memory: TypeAlias = Union[HexMemory, IQMemory]
TimestampLabel: TypeAlias = Literal["started", "finished"]
StageName: TypeAlias = Literal[
    "registration",
    "pre_processing",
    "execution",
    "post_processing",
    "final",
]


@unique
class Stage(int, Enum):
    """Stage in the BCC chain"""

    REG_Q = 0
    REG_W = 1
    PRE_PROC_Q = 2
    PRE_PROC_W = 3
    EXEC_Q = 4
    EXEC_W = 5
    PST_PROC_Q = 6
    PST_PROC_W = 7
    FINAL_Q = 8
    FINAL_W = 9

    @property
    def verbose_name(self) -> str:
        """The name of this stage in a verbose manner"""
        return _STAGE_VERBOSE_NAME_MAP[self]


class StorageID(str):
    """A special type of string with format {uuid}:::{duration}"""

    @cached_property
    def parts(self) -> Tuple[str, float]:
        """the components of this storage id as a tuple"""
        try:
            str_parts = self.split(_STORAGE_ID_SEPARATOR)
            uuid = str_parts[0]
            duration = float(str_parts[1])
            return uuid, duration
        except (IndexError, ValueError) as exp:
            raise ValueError(f"malformed key: {exp}")

    @property
    def duration(self) -> float:
        """the duration represented in this storage id"""
        return self.parts[1]

    @property
    def uuid(self) -> str:
        """the job id in this storage id"""
        return self.parts[0]

    @classmethod
    def from_job(cls, job: "Job") -> "StorageID":
        """Gets Storage ID from the job where uuid part is the job_id

        Duration defaults to 0 if it is None

        Args:
            job: the job
        Returns:
            the storage ID for this job
        """
        return cls.from_details(uuid=job.job_id, duration=job.estimated_duration)

    @classmethod
    def from_details(cls, uuid: str, duration: Optional[float]) -> "StorageID":
        """Constructs a storage id from just its details

        Args:
            uuid: the unique identifier
            duration: the optional duration; when it is None, it is set to 0

        Returns:
            the storage id
        """
        value = f"{uuid}{_STORAGE_ID_SEPARATOR}{duration or 0}"
        return cls(value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(cls, handler(str))


class TimestampPair(BaseModel):
    started: Optional[str] = None
    finished: Optional[str] = None
    _start_timestamp: Optional[datetime] = None
    _finish_timestamp: Optional[datetime] = None

    @computed_field
    @property
    def start_timestamp(self) -> Optional[datetime]:
        """started as a datetime"""
        if self._start_timestamp is None:
            try:
                self._start_timestamp = datetime.fromisoformat(self.started)
            except TypeError:
                pass

        return self._start_timestamp

    @computed_field
    @property
    def finish_timestamp(self) -> Optional[datetime]:
        """finished as a datetime"""
        if self._finish_timestamp is None:
            try:
                self._finish_timestamp = datetime.fromisoformat(self.finished)
            except TypeError:
                pass

        return self._finish_timestamp


class Timestamps(BaseModel):
    """Timestamps for the job"""

    pre_processing: Optional[TimestampPair] = None
    execution: Optional[TimestampPair] = None
    post_processing: Optional[TimestampPair] = None

    @property
    def resource_usage(self) -> Optional[float]:
        """the resource usage obtained from this timestamp"""
        try:
            return (
                self.execution.finish_timestamp - self.execution.start_timestamp
            ).total_seconds()
        except (TypeError, AttributeError):
            """
            TypeError: unsupported operand type(s) for -: 'datetime.datetime' and 'NoneType'
            TypeError: unsupported operand type(s) for -: 'NoneType' and 'datetime.datetime'
            TypeError: unsupported operand type(s) for -: 'NoneType' and 'NoneType'
            AttributeError: 'NoneType' object has no attribute 'started'
            AttributeError: 'NoneType' object has no attribute 'finished'
            """
            return None

    @property
    def current_time_used(self) -> Optional[float]:
        """Current time used by the job so far"""
        # Return the total resource usage if the job has already completed execution
        if self.execution.finish_timestamp is not None:
            return self.resource_usage

        try:
            return (get_utc_now() - self.execution.start_timestamp).total_seconds()
        except (TypeError, AttributeError):
            """
            TypeError: unsupported operand type(s) for -: 'datetime.datetime' and 'NoneType'
            TypeError: unsupported operand type(s) for -: 'NoneType' and 'datetime.datetime'
            TypeError: unsupported operand type(s) for -: 'NoneType' and 'NoneType'
            AttributeError: 'NoneType' object has no attribute 'started'
            AttributeError: 'NoneType' object has no attribute 'finished'
            """
            return None


class JobStatus(str, Enum):
    PENDING = "pending"
    EXECUTING = "executing"
    FAILED = "failed"
    SUCCESSFUL = "successful"
    CANCELLED = "cancelled"

    def is_terminal(self):
        """Whether the current stage is end of line i.e. cannot be changed"""
        return self in (JobStatus.CANCELLED, JobStatus.FAILED, JobStatus.SUCCESSFUL)


class JobResult(BaseModel):
    """The results of the job"""

    model_config = ConfigDict(extra="allow")
    memory: Memory = Field(default_factory=list)


class JobFileParams(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    qobj: PulseQobj

    @field_serializer("qobj")
    def serialize_qobj(self, qobj: PulseQobj, _info: SerializationInfo):
        """Converts qobj into a dict"""
        qobj_dict = qobj.to_dict()

        if _info.mode_is_json():
            return json.dumps(qobj_dict, cls=IQXJsonEncoder)
        return qobj_dict

    @field_validator("qobj", mode="before")
    @classmethod
    def parse_qobj(cls, v):
        """Parses the qobject from dict/str to Qobj"""
        if isinstance(v, PulseQobj):
            return v
        elif isinstance(v, dict):
            return PulseQobj.from_dict(v)
        elif isinstance(v, str):
            return PulseQobj.from_dict(json.loads(v))

        raise TypeError(f"Invalid type for PulseQobj: {type(v)}")


class JobFile(BaseModel):
    """The expected structure of the job file"""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    params: JobFileParams


class Job(Schema):
    """Representation of the job to be executed by this application

    Attributes:
        job_id: the unique identifier of this job
        user_id: the unique identifier of the user who owns this job
        estimated_duration: the estimated time this job could take to execute in seconds;
            default = None
        timestamps: the timestamps for each stage of running this job; default = None
        status: the status of this job; default = JobStatus.PENDING
        failure_reason: the reason why the job has failed; default = None
        type: the category in which this job falls, which thus determines the
            functions to preprocess or execute it; default = None
    """

    __primary_key_fields__ = ("job_id",)
    __index_fields__ = ("user_id", "status")

    model_config = ConfigDict(
        extra="allow", arbitrary_types_allowed=True, validate_assignment=True
    )

    job_id: str = Field(default_factory=uuid_str)
    device: str
    calibration_date: str
    user_id: Optional[str] = None
    estimated_duration: Optional[float] = None
    timestamps: Optional[Timestamps] = None
    stage: Stage = Stage.REG_Q
    status: JobStatus = JobStatus.PENDING
    failure_reason: Optional[str] = None
    cancellation_reason: Optional[str] = None
    storage_id: StorageID = None
    actual_duration: Optional[float] = None
    download_url: Optional[str] = None
    result: Optional[JobResult] = None
    created_at: Optional[str] = Field(default_factory=utc_now_str)
    updated_at: Optional[str] = Field(default_factory=utc_now_str)

    @property
    def start_utc(self) -> Optional[datetime]:
        """The start timestamp of the execution of the job in UTC timezone"""
        try:
            return to_utc(self.timestamps.execution.start_timestamp)
        except AttributeError:
            return None

    @property
    def end_utc(self) -> Optional[datetime]:
        """The end timestamp of the execution of the job in UTC timezone"""
        try:
            return to_utc(self.timestamps.execution.finish_timestamp)
        except AttributeError:
            return None

    @property
    def current_eta(self) -> Optional[float]:
        """Current estimated time to completion

        Raises:
            JobAlreadyCompleteError: job '{self.job_id}' is already complete
        """
        if self.actual_duration is not None:
            raise JobAlreadyCompleteError(f"job '{self.job_id}' is already complete")

        try:
            return self.estimated_duration - self.timestamps.current_time_used
        except (TypeError, AttributeError):
            """
            TypeError: unsupported operand type(s) for -: 'float' and 'NoneType'
            TypeError: unsupported operand type(s) for -: 'NoneType' and 'float'
            TypeError: unsupported operand type(s) for -: 'NoneType' and 'NoneType'
            """
            return None

    @model_validator(mode="after")
    def validate_model(self):
        """Validate some computed fields"""
        # set the actual_duration
        try:
            if self.actual_duration is None and self.timestamps.resource_usage:
                self.actual_duration = self.timestamps.resource_usage
        except AttributeError:
            pass

        # set the storage id
        if self.storage_id is None:
            # we don't want to automatically reset this say on a partial model
            # that may have no duration or job_id.
            # We should update it however if either job_id or estimated_duration is updated
            if (
                not isinstance(self, PartialMeta)
                or self.job_id
                or self.estimated_duration
            ):
                self.storage_id = StorageID.from_job(self)

        return self

    @field_validator(
        "storage_id", mode="before", json_schema_input_type=Union[StorageID, str]
    )
    @classmethod
    def cast_storage_id(cls, value: Any) -> Any:
        if isinstance(value, str):
            return StorageID(value)
        return value


@unique
class LogLevel(Enum):
    """Log level of job supervisor log messages"""

    INFO = 0
    WARNING = 1
    ERROR = 2


class QueueContext(TypedDict):
    """Options passed to all queue callback creators

    Attributes:
        queue_prefix: the prefix attached to all queues
        booking_db_url: the URL to the database containing bookings
        jobs_store_url: the URL to the store containing the jobs
        force_normal_queue: the flag for whether to force the usage of the normal queue
        max_idle_time: the maximum time a booking can remain idle
        is_async: whether jobs should be run in async workers or in the same process; good for testing
    """

    queue_prefix: str
    booking_db_url: str
    jobs_store_url: str
    force_normal_queue: NotRequired[bool]
    post_processing_folder: str
    max_idle_time: int
    is_async: bool


_STAGE_VERBOSE_NAME_MAP: Dict[Stage, str] = {
    Stage.REG_Q: "registration queue",
    Stage.REG_W: "registration worker",
    Stage.PRE_PROC_Q: "pre-processing queue",
    Stage.PRE_PROC_W: "pre-processing worker",
    Stage.EXEC_Q: "execution queue",
    Stage.EXEC_W: "execution worker",
    Stage.PST_PROC_Q: "post-processing queue",
    Stage.PST_PROC_W: "post-processing worker",
    Stage.FINAL_Q: "finalization queue",
    Stage.FINAL_W: "finalization worker",
}
