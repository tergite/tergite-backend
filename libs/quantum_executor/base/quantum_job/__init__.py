# This code is part of Tergite
#
# (C) Copyright Axel Andersson 2022
# (C) Copyright Martin Ahindura 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
# Refactored by Chalmers Next Labs 2025

import json
import re
from collections import namedtuple
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Match,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeAlias,
    TypeVar,
    Union,
)

import h5py
import numpy as np
import xarray as xr
from numpy import typing as npt

from libs.qiskit.qobj import PulseQobj, PulseQobjInstruction

from ....device_parameters.dtos import DeviceCalibration
from ...utils.general import search_nested
from ..circuits import CircuitReadout
from .dtos import (
    ByteOrder,
    MeasLvl,
    MeasRet,
    QobjData,
    QobjHeaderMetadata,
    QobjSweepData,
    QuantumJob,
    SweepParamMetadata,
)
from .typing import QDataset, QJobResult

T = TypeVar("T")

IQPoint: TypeAlias = Tuple[float, float]  # [re, im]  (len = 2)
IQMemory: TypeAlias = List[List[List[IQPoint]]]  # exp → shot → IQ points
HexMatrix = List[List[str]]
AcqParams = namedtuple("AcqParams", "qubits memory_slots")


_KEY_DELIMITER = "~"
_HDF5_JOB_RESULTS_PATH_REGEX = re.compile(
    rf"experiments/(.*)/slot{_KEY_DELIMITER}(\d+)/measurement"
)
_HDF5_QOBJ_METADATA_PATH = "header/qobj_metadata"
_HDF5_QOBJ_DATA_PATH = "header/qobj_data"
_HDF5_HEADER_METADATA_PATH = "header/qobj/backend"
_HDF5_SWEEP_DATA_PATH = "header/qobj/sweep"
_HDF5_READOUTS_PATH = "header/readouts"


_dec_to_hex = np.frompyfunc(
    hex, nin=1, nout=1
)  # type: Callable[[npt.NDArray[np.int64]], npt.NDArray[np.str_]]
"""Converts a numpy 1D array of decimal numbers to a numpy array of hex strings

Args:
    __number: the numpy array of decimals

Returns:
    the numpy 1D array of hex strings
"""


def read_job_from_hdf5(file: Path) -> QuantumJob:
    """Extract the quantum job from the hdf5 file

    Args:
        file: the path to the file

    Returns:
        the quantum job saved in the hdf5 file
    """
    with h5py.File(file, mode="r") as hdf5_file:
        tuid = hdf5_file.attrs["tuid"]
        meas_return = MeasRet(hdf5_file.attrs["meas_return"])
        meas_level = MeasLvl(hdf5_file.attrs["meas_level"])
        meas_return_cols = hdf5_file.attrs["meas_return_cols"]
        n_qubits = hdf5_file.attrs["n_qubits"]
        job_id = hdf5_file.attrs.get("job_id")
        raw_results = _extract_results_from_hdf5(hdf5_file)
        qobj = _read_qobj_from_hdf5(hdf5_file)
        readouts = _read_readouts_from_hdf5(hdf5_file)

    return QuantumJob(
        tuid=tuid,
        meas_return=meas_return,
        meas_level=meas_level,
        meas_return_cols=meas_return_cols,
        n_qubits=n_qubits,
        raw_results=raw_results,
        job_id=job_id,
        qobj=qobj,
        readouts=readouts,
    )


def save_job_in_hdf5(job: QuantumJob, file: Path):
    """Saves this job to an HDF5 file

    Args:
        job: the QuantumJob to save to HDF5 file.
        file: the path to the file where the data is to be saved
    """
    with h5py.File(file, mode="w") as hdf5_file:
        hdf5_file.attrs["tuid"] = job.tuid
        hdf5_file.attrs["meas_return"] = job.meas_return.value
        hdf5_file.attrs["meas_level"] = job.meas_level.value
        hdf5_file.attrs["meas_return_cols"] = job.meas_return_cols
        hdf5_file.attrs["n_qubits"] = job.n_qubits
        hdf5_file.attrs["memory_slot_size"] = job.memory_slot_size
        hdf5_file.attrs["job_id"] = job.job_id
        hdf5_file.attrs["local"] = job.is_local

        if job.qobj is not None:
            header_dict = job.qobj.header.to_dict()
            _save_qobj_header_to_hdf5(hdf5_file, header_dict=header_dict)
            _save_sweep_data_to_hdf5(hdf5_file, header_dict=header_dict)
            _save_qobj_to_hdf5(hdf5_file, job=job)
        if job.readouts is not None:
            _save_readouts_to_hdf5(hdf5_file, readouts=job.readouts)
        _save_results_to_hdf5(hdf5_file, job.raw_results)


def get_acquisition_parameters_from_experiment(
    exp_index: int,
    qobj: "PulseQobj",
) -> AcqParams:
    """
    Extract the qubit indices and memory-slot indices used by *acquire*
    instructions in a Pulse experiment.

    Args:
        exp_index: index of the experiment in `qobj.experiments`.
        qobj: a :class:`PulseQobj` containing the experiments.
    Returns:
        AcqParams: named tuple of `(qubits, memory_slots)`.
    Raises:
        IndexError: if `exp_index` is out of range.
        ValueError: if no acquire instruction is found
    """

    # index sanity-check
    try:
        experiment_obj = qobj.experiments[exp_index]
    except IndexError:
        raise IndexError(
            f"Experiment index {exp_index} out of range (0 ... {len(qobj.experiments) - 1})."
        )

    acq_instrs = [
        inst for inst in experiment_obj.instructions if inst.name == "acquire"
    ]
    if not acq_instrs:
        raise ValueError(f"Experiment {exp_index} contains no 'acquire' instruction.")
    return _collect_from_obj(acq_instrs)


def discriminate_results(
    job: QuantumJob,
    discriminator: Callable[[int, npt.NDArray[np.complexfloating]], int],
    *,
    num_of_states: Union[Literal[2], Literal[3]] = 2,
    byteorder: ByteOrder = ByteOrder.LITTLE_ENDIAN,
    calibration: DeviceCalibration = None,
    **kwargs,
) -> HexMatrix:
    """
    Convert raw IQ data in `job.raw_results` into hexadecimal read-outs.

    Steps:
    1. Extract `acquire` mappings (qubit -> classical slot once per experiment)
    2. For every acquisition channel, run the supplied `discriminator` to obtain discriminated 0/1/2 int per shot
    3. Pack the values into a register-shaped array (`c_reg_length` x `shots`), respecting qubit->slot map
    4. Convert each register state (row) to a hex string

    Args:
        job: quantum job whose results are to be discriminated
        discriminator: a function which takes two arguments "qubit_index" and "iq_points" (an array)
            and returns a binary value (0/1).
        num_of_states: the number of states the discriminator produces; default=2
        byteorder: the byte order of the acquisition channel list; default=ByteOrder.LITTLE_ENDIAN
        calibration: the device calibration object; default=None

    Returns:
        HexMatrix `results[experiments][shot]` -> hex string
        Nested list of the hex (base 16) representations of the states e.g. 0, 1.
        Each inner list corresponds to one experiment.
        Each item in the inner list corresponds to a shot number
    """
    if not callable(discriminator):
        raise ValueError("'discriminator' must be callable")

    qubit_index_map = _get_sdk_qubit_index_map(calibration)
    return [
        _discriminate_experiment(
            job,
            exp_index,
            dataset,
            discriminator=discriminator,
            qubit_index_map=qubit_index_map,
            num_of_states=num_of_states,
            byteorder=byteorder,
        )
        for exp_index, dataset in enumerate(job.raw_results.values())
    ]


def xarray_to_list(job: QuantumJob) -> IQMemory:
    """
    Convert `job.raw_results` into the nested-list structure expected by `JobResult(memory=...)`
    for measurement-level-1 (INTEGRATED) data.

    The returned structure:

        memory  ->  List[                       # experiments
                        List[                    # repetitions / shots
                            List[complex, ...]   # channel (and acquisition) results
                        ]
                    ]

    NumPy complex scalars are converted to float tuple.

    Args:
        job: the quantum job whose raw results are to be converted

    Returns:
        the memory of the job, per experiment, per shot, per channel

    Raises:
        ValueError: the job has no raw results
        TypeError: a raw result is not an xarray.Dataset
    """
    if job.raw_results is None:
        raise ValueError(
            "xarray_to_list: Can't find Quantum job experiment results `job.raw_results`."
        )

    memory: IQMemory = []
    for exp_index, dataset in enumerate(job.raw_results.values()):
        if not isinstance(dataset, xr.Dataset):
            raise TypeError(
                "xarray_to_list: expected an xarray.Dataset in raw_results values"
            )
        channel_ids = _get_ordered_channel_ids(dataset, job.qobj, exp_index)
        shots = dataset.sizes.get("repetition", 1)
        memory.append(
            [_get_shot_iq_points(dataset, channel_ids, shot) for shot in range(shots)]
        )
    return memory


def discriminate_circuit_results(
    job: QuantumJob,
    discriminator: Callable[[int, npt.NDArray[np.complexfloating]], int],
    *,
    num_of_states: Union[Literal[2], Literal[3]] = 2,
) -> HexMatrix:
    """Discriminates a gate-level job's IQ points into each shot's classical register

    Each measurement's states go to the classical bit its readout names, a later
    measurement of a bit replacing an earlier one; bit 0 is the least significant.

    Args:
        job: the gate-level job whose results are to be discriminated
        discriminator: a function which takes the number in a qubit's id and its IQ
            points, and returns their states
        num_of_states: the number of states the discriminator produces; default=2

    Returns:
        the hex of each shot's register, per experiment

    Raises:
        ValueError: the job has {n} readouts for {m} experiment results
    """
    return [
        _discriminate_circuit(readout, dataset, discriminator, num_of_states)
        for readout, dataset in _get_readouts_and_results(job)
    ]


def get_circuit_iq_memory(job: QuantumJob) -> IQMemory:
    """Gets a gate-level job's IQ points, per experiment, per shot, per measured classical
    bit in order, each from the bit's last measurement

    Args:
        job: the gate-level job whose results are to be converted

    Returns:
        the memory of the job; one shot per experiment if its shots are averaged

    Raises:
        ValueError: the job has {n} readouts for {m} experiment results
    """
    return [
        _get_circuit_iq_points(readout, dataset)
        for readout, dataset in _get_readouts_and_results(job)
    ]


def get_experiment_name(qobj_expt_name: str, expt_index: int):
    """
    Creates a cleaned version of a given experiment name

    Args:
        qobj_expt_name: the name as got from the qobject
        expt_index: the index of the experiment in the list of experiments in the qobject

    Returns:
        a sanitized name to use internally
    """
    name = "".join(x for x in qobj_expt_name if x.isalnum() or x in " -_,.()")
    return f"{name}{_KEY_DELIMITER}{expt_index}"


def _save_qobj_header_to_hdf5(file: h5py.File, header_dict: dict):
    """Saves the Qobj header metadata to the HDF5 file

    Args:
        file: the HDF5 file to save to
        header_dict: the dict from QobjHeader
    """
    backend_metadata = QobjHeaderMetadata.from_qobj_header(header_dict).model_dump()
    _save_hdf5_attributes(
        file, path=_HDF5_HEADER_METADATA_PATH, source=backend_metadata
    )


def _save_qobj_to_hdf5(file: h5py.File, job: QuantumJob):
    """Saves the Qobj data and metadata to the HDF5 file

    Args:
        file: the HDF5 file to save to
        job: the quantum job containing the qobj
    """
    if job.qobj is None:
        return
    _save_hdf5_attributes(
        file, path=_HDF5_QOBJ_METADATA_PATH, source=job.metadata.to_dict()
    )
    _save_hdf5_attributes(
        file,
        path=_HDF5_QOBJ_DATA_PATH,
        source=QobjData.from_qobj(job.qobj).to_dict(),
    )


def _save_readouts_to_hdf5(file: h5py.File, readouts: List[CircuitReadout]) -> None:
    """Saves a gate-level job's readouts, in the order of its experiments, as JSON

    Args:
        file: the HDF5 file to save to
        readouts: the readouts of the job's experiments
    """
    group = file.require_group(_HDF5_READOUTS_PATH)
    group.attrs["readouts"] = json.dumps([readout.to_dict() for readout in readouts])


def _read_qobj_from_hdf5(file: h5py.File) -> Optional[PulseQobj]:
    """Reads the job's qobj from the HDF5 file, or None if it is a gate-level job's"""
    if _HDF5_QOBJ_DATA_PATH not in file:
        return None
    qobj_data = _read_hdf5_attributes(file, path=_HDF5_QOBJ_DATA_PATH, type_=QobjData)
    return qobj_data.to_qobj()


def _read_readouts_from_hdf5(file: h5py.File) -> Optional[List[CircuitReadout]]:
    """Reads a gate-level job's readouts from the HDF5 file, or None if it has a qobj"""
    if _HDF5_READOUTS_PATH not in file:
        return None
    readouts = json.loads(file[_HDF5_READOUTS_PATH].attrs["readouts"])
    return [CircuitReadout.from_dict(readout) for readout in readouts]


def _save_sweep_data_to_hdf5(file: h5py.File, header_dict: dict):
    """Saves the sweep data and metadata to the HDF5 file

    Args:
        file: the HDF5 file to save to
        header_dict: the dict from QobjHeader
    """
    try:
        sweep_data = QobjSweepData.from_qobj_header(header_dict)
    except ValueError:
        return

    _save_hdf5_attributes(file, path=_HDF5_SWEEP_DATA_PATH, source=sweep_data.metadata)

    sweep_data_dict = sweep_data.model_dump()
    for path_segments in search_nested(sweep_data_dict, "slots"):
        sweep_group = file.require_group(_HDF5_SWEEP_DATA_PATH)
        slots_path = "/".join(path_segments)
        slots_group = sweep_group.create_group(slots_path)

        # -1 is "slots", -2 is parameter name, -3 is "parameters"
        param = path_segments[-2]
        param_group_path = f"{_HDF5_SWEEP_DATA_PATH}/parameters/{param}"
        param_metadata = SweepParamMetadata(
            **sweep_data_dict["parameters"][param]
        ).model_dump()
        _save_hdf5_attributes(file, path=param_group_path, source=param_metadata)

        slots_dict = _get_value_at_path(sweep_data_dict, path_segments)
        # store all specified sweep parameter data in respective HDF datasets
        for slot_idx, slot_data in slots_dict.items():
            key = f"slot{_KEY_DELIMITER}{slot_idx}"
            slots_group.create_dataset(key, data=np.asarray(slot_data))


def _save_results_to_hdf5(file: h5py.File, results: QJobResult):
    """Saves the experiment results to the HDF5 file

    The results for acquisition channel ``i`` in experiment of name ``name`` are saved at
    path ``experiments/{name}/slot~{i}/measurement`` in the file

    Args:
        file: the HDF5 file to save to
        results: the experiment results to save
    """
    if "experiments" not in file:
        file.create_group("experiments", track_order=True)
    for name, result in results.items():
        path = f"experiments/{name}"

        for acq_index, acq in enumerate(result.data_vars):
            channel = f"slot{_KEY_DELIMITER}{acq}"
            data_path = f"{path}/{channel}/measurement"
            data_array = result[acq]

            h5_dataset = file.require_dataset(
                data_path,
                shape=data_array.shape,
                dtype=data_array.dtype,
            )

            h5_dataset[...] = data_array


def _extract_results_from_hdf5(
    file: h5py.File,
) -> QJobResult:
    """Retrieves the experiment results from the HDF5 file

    Args:
        file: the HDF5 file to save to

    Returns:
        dict of the results with keys as experiment name and values as xarray.Dataset
    """
    measurement_paths = _match_hdf5_paths(file, pattern=_HDF5_JOB_RESULTS_PATH_REGEX)

    results: QJobResult = {}
    for path_match in measurement_paths:
        path = path_match.group(0)
        expt_name = path_match.group(1)
        acq = path_match.group(2)

        xarray_dataset = results.get(expt_name)
        if xarray_dataset is None:
            results[expt_name] = xarray_dataset = QDataset()

        data = file[path][:]
        # quantify averages the repetitions away, leaving no dimension for them
        dims = [f"acq_index_{acq}"]
        if data.ndim == 2:
            dims = ["repetition", f"acq_index_{acq}"]
        xarray_dataset[acq] = (dims, data)

    # Sort by the numeric experiment index embedded in the name (e.g. "circuit~3" -> 3).
    # HDF5 visit() traverses groups alphabetically, so without this "~10" would
    # come before "~2", causing discriminate_results / xarray_to_list to look up
    # the wrong qobj.experiments[i] for every experiment after the 9th.
    return dict(sorted(results.items(), key=lambda kv: _get_experiment_order(kv[0])))


def _match_hdf5_paths(file: h5py.File, pattern: re.Pattern) -> List[Match]:
    """Gets the HDF5 paths that match the given pattern

    Args:
        file: the HDF5 file
        pattern: the regex pattern to test against

    Returns:
        the list of path matches for the pattern
    """
    measurement_paths: List[Match] = []

    def collect_path_matches(name: str):
        if match := pattern.match(name):
            measurement_paths.append(match)

    file.visit(collect_path_matches)

    return measurement_paths


def _save_hdf5_attributes(file: h5py.File, path: str, source: dict):
    """Saves the whole dict to HDF5 attributes at the given path

    Args:
        file: the HDF5 file
        path: the /-separated path to the group
        source: the dictionary to copy from
    """
    if len(source) == 0:
        return

    if path not in file:
        group = file.create_group(path, track_order=True)
    else:
        group = file[path]

    for key, value in source.items():
        group.attrs[key] = value


def _read_hdf5_attributes(file: h5py.File, path: str, type_: Type[T] = dict) -> T:
    """Reads the HDF5 attributes at the given path

    Args:
        file: the HDF5 file
        path: the /-separated path to the group
        type_: the type of the returned instance

    Returns:
        the metadata (attrs) saved at the given path, cast to the given type_

    Raises:
        KeyError: `path`
    """
    group = file[path]
    return type_(**group.attrs)


def _get_value_at_path(data: dict, path: List[str]) -> Any:
    """Retrieves the value at the given path of the nested dict data

    e.g. ["foo", "bar", "py"] return data["foo"]["bar"]["py"]

    Args:
        data: the nested dictionary
        path: the path to the value needed

    Returns:
        the value at the given path
    """
    value = data
    for part in path:
        value = data[part]

    return value


def _bitarrays_to_decimal(
    array: npt.NDArray[np.int8],
    base: int,
    byteorder: ByteOrder = ByteOrder.LITTLE_ENDIAN,
):
    """
    Convert a 2D array in any base to integers in base 10 with selectable byte order.

    Parameters:
        array: Input 2D array of integers in the specified base.
        base: The base of the input array (e.g., 2 for binary, 3 for base-3).
        byteorder: 'big-endian' (MSD first), or 'little-endian' (LSD first).

    Returns:
        numpy.ndarray: 1D array of integers representing each row.
    """
    # Flip the array for little-endian (LSD first)
    if byteorder == ByteOrder.LITTLE_ENDIAN:
        array = array[:, ::-1]

    # Compute the powers of the base, e.g. for base 3 => [3^2, 3^1, 3^0]
    powers_of_base = base ** np.arange(array.shape[1] - 1, -1, -1)

    # Convert each row to integers (base 10)
    integers = array @ powers_of_base

    return integers


def _get_q_to_c_from_qobj(
    qobj: "PulseQobj",
    exp_index: int,
    acq: AcqParams,
) -> dict[int, int]:
    """
    sdk_qubit_index -> circuit_clbit_index

    Prefer explicit qobj metadata, fall back to acquire(qubits, memory_slot).
    """
    tergite_md = _get_tergite_metadata_for_experiment(qobj, exp_index)
    raw_q_to_c = tergite_md.get("q_to_c")

    if raw_q_to_c:
        return {int(q): int(c) for q, c in raw_q_to_c.items()}

    return {int(q): int(c) for q, c in zip(acq.qubits, acq.memory_slots)}


def _get_c_reg_len_from_qobj(
    job: QuantumJob,
    qobj: "PulseQobj",
    exp_index: int,
    acq: AcqParams,
) -> int:
    """
    Per-experiment classical width.

    Preference order:
      1. tergite metadata c_reg_len
      2. inferred from acquire.memory_slot
      3. job.n_qubits
    """
    tergite_md = _get_tergite_metadata_for_experiment(qobj, exp_index)

    c_reg_len = tergite_md.get("c_reg_len")
    if c_reg_len is not None:
        return int(c_reg_len)

    if acq.memory_slots:
        return max(int(slot) for slot in acq.memory_slots) + 1

    return int(job.n_qubits)


def _get_tergite_metadata_for_experiment(
    qobj: "PulseQobj", exp_index: int
) -> dict[str, Any]:
    exp = qobj.experiments[exp_index]
    header = getattr(exp, "header", None)
    metadata = getattr(header, "metadata", None) or {}

    if not isinstance(metadata, dict):
        return {}

    tergite_md = metadata.get("tergite", {})
    return tergite_md if isinstance(tergite_md, dict) else {}


def _get_sdk_qubit_index_map(calibration: DeviceCalibration) -> Dict[int, int]:
    """Maps each calibrated qubit's id to its index in the SDK, its place among the ids
    in order"""
    qubit_ids = sorted(int(qubit.id) for qubit in calibration.qubits)
    return {qubit_id: index for index, qubit_id in enumerate(qubit_ids)}


def _discriminate_experiment(
    job: QuantumJob,
    exp_index: int,
    dataset: QDataset,
    discriminator: Callable[[int, npt.NDArray[np.complexfloating]], int],
    qubit_index_map: Dict[int, int],
    num_of_states: int,
    byteorder: ByteOrder,
) -> List[str]:
    """Discriminates the experiment's acquisitions into the classical bits their qubits
    are measured into, and gets each shot's register as hex"""
    qobj = job.qobj
    acq = get_acquisition_parameters_from_experiment(exp_index=exp_index, qobj=qobj)
    q_to_c = _get_q_to_c_from_qobj(qobj, exp_index, acq)
    c_reg_len = _get_c_reg_len_from_qobj(
        job=job, qobj=qobj, exp_index=exp_index, acq=acq
    )
    shots: int = dataset.sizes.get("repetition", 1)

    states_by_cbit = {}
    unmapped_channels: list[str] = []
    # as acquisitions are always ordered, qubit id would correspond to sdk qubit id per base contract
    for channel, acquisitions in sorted(dataset.items(), key=lambda kv: int(kv[0])):
        if acquisitions.shape[1] != 1 and job.meas_return != MeasRet.AVERAGED:
            raise ValueError(
                f"Experiment data contain more than one measurement per channel. "
                f"Found {acquisitions.shape[1]} for channel {channel}"
            )

        quantify_qubit_id = int(channel)
        if quantify_qubit_id not in qubit_index_map:
            raise ValueError(
                f"Can't find qubit id {quantify_qubit_id} in the calibration data."
                f"{qubit_index_map}"
            )
        sdk_qubit_id = qubit_index_map[quantify_qubit_id]
        if sdk_qubit_id not in q_to_c:
            unmapped_channels.append(sdk_qubit_id)
            continue

        states = discriminator(quantify_qubit_id, acquisitions.data[:, 0])
        if not np.isscalar(states) and len(states) != shots:
            raise ValueError(
                f"Discriminator for qubit {sdk_qubit_id} returned {len(states)} "
                f"values, expected {shots}"
            )
        states_by_cbit[int(q_to_c[sdk_qubit_id])] = states

    if unmapped_channels:
        raise ValueError(
            "Could not map raw acquisition channel ids "
            f"{unmapped_channels} to SDK qubit ids for experiment "
            f"{exp_index!r}."
            f"{q_to_c}"
            f"{qubit_index_map}"
        )

    register = _get_register(states_by_cbit, c_reg_len=c_reg_len, shots=shots)
    return _to_hex_shots(register, num_of_states=num_of_states, byteorder=byteorder)


def _get_register(
    states_by_cbit: Dict[int, Any], c_reg_len: int, shots: int
) -> np.ndarray:
    """Gets the classical register, a row per bit and a column per shot, with each bit's
    discriminated states, one for all shots or one per shot; a bit no qubit is measured
    into stays 0"""
    register = np.zeros((c_reg_len, shots), dtype=np.int8)
    for cbit, states in states_by_cbit.items():
        register[cbit, :] = (
            states if np.isscalar(states) else np.asarray(states, dtype=np.int8)
        )
    return register


def _to_hex_shots(
    register: np.ndarray, num_of_states: int, byteorder: ByteOrder
) -> List[str]:
    """Gets the register's state in each shot as hex"""
    decimals = _bitarrays_to_decimal(
        register.transpose(), base=num_of_states, byteorder=byteorder
    )
    return _dec_to_hex(decimals).tolist()


def _get_ordered_channel_ids(
    dataset: QDataset, qobj: PulseQobj, exp_index: int
) -> List[int]:
    """Gets the dataset's channels, the physical qubit ids, in the order of the memory
    slots they are measured into if each is, or else of the ids"""
    channel_ids = sorted(int(key) for key in dataset.data_vars.keys())
    try:
        acq = get_acquisition_parameters_from_experiment(exp_index=exp_index, qobj=qobj)
    except (IndexError, ValueError):
        # the experiment is missing, or measures nothing
        return channel_ids

    qubit_to_slot = dict(zip(acq.qubits, acq.memory_slots))
    if qubit_to_slot and all(qubit in qubit_to_slot for qubit in channel_ids):
        return sorted(channel_ids, key=lambda qubit: int(qubit_to_slot[qubit]))
    return channel_ids


def _get_shot_iq_points(
    dataset: QDataset, channel_ids: List[int], shot: int
) -> List[IQPoint]:
    """Gets the IQ points of each channel's acquisitions in the shot, channel by channel"""
    points: List[IQPoint] = []
    for channel_id in channel_ids:
        data = dataset[str(channel_id)].data
        if data.ndim == 2:
            row = data[shot, ...]
        elif data.ndim == 1:
            row = data
        else:
            raise ValueError(
                f"xarray_to_list: unexpected ndarray shape {data.shape} for channel {channel_id}"
            )
        points.extend(_to_iq_point(value) for value in np.ravel(row))
    return points


def _to_iq_point(value: np.generic) -> IQPoint:
    """Gets the real and imaginary parts of the value, or (0, 0) if either is not finite"""
    point = complex(value.item())
    if not np.isfinite([point.real, point.imag]).all():
        point = 0j
    return (float(point.real), float(point.imag))


def _get_readouts_and_results(
    job: QuantumJob,
) -> List[Tuple[CircuitReadout, QDataset]]:
    """Pairs each experiment's readout with its results

    Raises:
        ValueError: the job has {n} readouts for {m} experiment results
    """
    results = list(job.raw_results.values())
    if len(job.readouts) != len(results):
        raise ValueError(
            f"the job has {len(job.readouts)} readouts for {len(results)} experiment results"
        )
    return list(zip(job.readouts, results))


def _discriminate_circuit(
    readout: CircuitReadout,
    dataset: QDataset,
    discriminator: Callable[[int, npt.NDArray[np.complexfloating]], int],
    num_of_states: int,
) -> List[str]:
    """Discriminates each channel's acquisitions into the classical bit its measurement
    names, and gets each shot's register as hex"""
    shots: int = dataset.sizes.get("repetition", 1)
    states_by_cbit = {}
    for channel, measurement in enumerate(readout.measurements):
        acquisitions = dataset[str(channel)].data
        states_by_cbit[measurement.clbit] = discriminator(
            measurement.qubit_id, acquisitions[:, 0]
        )
    register = _get_register(states_by_cbit, c_reg_len=readout.num_clbits, shots=shots)
    return _to_hex_shots(
        register, num_of_states=num_of_states, byteorder=ByteOrder.LITTLE_ENDIAN
    )


def _get_circuit_iq_points(
    readout: CircuitReadout, dataset: QDataset
) -> List[List[IQPoint]]:
    """Gets each shot's IQ points, one per measured classical bit, in the bits' order"""
    channels_by_clbit = {
        measurement.clbit: channel
        for channel, measurement in enumerate(readout.measurements)
    }
    channel_ids = [channels_by_clbit[clbit] for clbit in sorted(channels_by_clbit)]
    shots = dataset.sizes.get("repetition", 1)
    return [_get_shot_iq_points(dataset, channel_ids, shot) for shot in range(shots)]


def _get_experiment_order(expt_name: str) -> Tuple[int, int, str]:
    """Gets where the experiment sorts: by the index in its name, and those without one
    after, by name; an index and a name never compare, as an int and a str do not"""
    index = _parse_exp_index_from_name(expt_name)
    return (0, index, "") if index is not None else (1, 0, expt_name)


def _collect_from_obj(acq_instrs: Sequence["PulseQobjInstruction"]) -> AcqParams:
    """Gather qubits / memory_slots from a sequence of `object` instructions."""
    qubits: List[int] = []
    memory_slots: List[int] = []

    for inst in acq_instrs:
        if not hasattr(inst, "qubits") or not hasattr(inst, "memory_slot"):
            raise ValueError(
                f"Acquire instruction @t0={getattr(inst, 't0', '-')} "
                "is missing 'qubits' or 'memory_slot'."
            )

        if len(inst.qubits) != len(inst.memory_slot):
            raise ValueError(
                f"Acquire instruction @t0={getattr(inst, 't0', '-')} "
                "has mismatched list lengths "
                f"(qubits={len(inst.qubits)}, memory_slot={len(inst.memory_slot)})."
            )

        qubits.extend(inst.qubits)
        memory_slots.extend(inst.memory_slot)

    return AcqParams(qubits=qubits, memory_slots=memory_slots)


def _parse_exp_index_from_name(expt_name: str, delimiter: str = "~") -> Optional[int]:
    """
    Your backend uses get_experiment_name(name, idx) => f"{name}~{idx}"
    This extracts that trailing idx. Returns None if parsing fails.
    """
    if delimiter not in expt_name:
        return None
    try:
        _, idx_str = expt_name.rsplit(delimiter, 1)
        return int(idx_str)
    except Exception:
        return None
