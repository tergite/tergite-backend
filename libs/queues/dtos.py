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
import dataclasses
from datetime import datetime
from enum import Enum, unique
from os import PathLike
from typing import (
    Any,
    Dict,
    List,
    Literal,
    Optional,
    Tuple,
    TypeAlias,
    Union,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    model_validator,
)

from utils.datetime import to_utc, utc_now_str
from utils.strings import uuid_str

from ..device_parameters import BackendConfig

HexMemory: TypeAlias = List[List[str]]
# IQ point = tuple(re, im) or list[re, im]  →  use Sequence[float] for both.
IQPoint: TypeAlias = Tuple[float, float] | List[float]
IQMemory: TypeAlias = List[List[List[IQPoint]]]
Memory: TypeAlias = Union[HexMemory, IQMemory]
JobEvent: TypeAlias = Literal["started", "finished"]
JobStage: TypeAlias = Literal[
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


class TimestampPair(BaseModel):
    """When a stage of the job started and finished, as ISO strings"""

    started: Optional[str] = None
    finished: Optional[str] = None

    @computed_field
    @property
    def start_timestamp(self) -> Optional[datetime]:
        """started, as a datetime"""
        return None if self.started is None else datetime.fromisoformat(self.started)

    @computed_field
    @property
    def finish_timestamp(self) -> Optional[datetime]:
        """finished, as a datetime"""
        return None if self.finished is None else datetime.fromisoformat(self.finished)


class Timestamps(BaseModel):
    """Timestamps for the job"""

    registration: Optional[TimestampPair] = None
    pre_processing: Optional[TimestampPair] = None
    execution: Optional[TimestampPair] = None
    post_processing: Optional[TimestampPair] = None
    final: Optional[TimestampPair] = None

    @property
    def resource_usage(self) -> Optional[float]:
        """the resource usage obtained from this timestamp"""
        try:
            return (
                self.execution.finish_timestamp - self.execution.start_timestamp
            ).total_seconds()
        except (TypeError, AttributeError):
            # the execution, or one of its timestamps, is missing
            return None


class JobStatus(str, Enum):
    """The overall status of the job, coarser than its stage"""

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


class Job(BaseModel):
    """Representation of the job to be executed by this application

    Attributes:
        job_id: the unique identifier of this job
        user_id: the unique identifier of the user who owns this job
        estimated_duration: the estimated time this job could take to execute in seconds;
            default = None
        timestamps: the timestamps for each stage of running this job; default = None
        status: the status of this job; default = JobStatus.PENDING
        failure_reason: the reason why the job has failed; default = None
        etc.
    """

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

    @model_validator(mode="before")
    @classmethod
    def remove_computed_fields(cls, data: Any) -> Any:
        """Removes the computed fields that a dumped job carries, which extra would keep

        Args:
            data: the raw data of the job

        Returns:
            the data without the computed fields
        """
        if not isinstance(data, dict):
            return data
        return {k: v for k, v in data.items() if k not in cls.model_computed_fields}

    @computed_field
    @property
    def actual_duration(self) -> Optional[float]:
        """The seconds the job took to execute, once it has"""
        if self.timestamps is None:
            return None
        return self.timestamps.resource_usage


@dataclasses.dataclass(frozen=True, slots=True)
class ExecutorOptions:
    """Key word args necessary for initializing an executor

    Attributes:
        executor_type: the executor type to return
        quantify_config_file: the path to the quantify configuration file of the executor
        quantify_metadata_file: the path to the quantify metadata file of the executor
        backend_config: the backend configuration of the executor
        backend_name: name of backend
        should_restore_currents: whether the executor should restore SPI currents
        is_cluster_reset_allowed: whether the clusters for this executor can be reset
        data_directory: the directory where to save experiment data
        calibration_node_config: the configuration file for the nodes during calibration
        calibration_device_config: the configuration file for the entire devices during calibration
        calibration_spi_config: the configuration file for the spi during calibration
    """

    executor_type: str
    backend_name: str
    backend_config: BackendConfig
    quantify_config_file: Optional[PathLike] = None
    quantify_metadata_file: Optional[PathLike] = None
    should_restore_currents: bool = False
    is_cluster_reset_allowed: bool = False
    data_directory: Optional[PathLike[str]] = None
    calibration_node_config: Optional[PathLike[str]] = None
    calibration_device_config: Optional[PathLike[str]] = None
    calibration_spi_config: Optional[PathLike[str]] = None
    calibration_seed_file: Optional[PathLike[str]] = None


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
