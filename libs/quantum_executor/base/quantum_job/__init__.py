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
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Match,
    Optional,
    Tuple,
    TypeAlias,
    Union,
)

import h5py
import numpy as np
from numpy import typing as npt

from ..circuits import CircuitReadout
from .dtos import ByteOrder, MeasLvl, MeasRet, QuantumJob
from .typing import QDataset, QJobResult

IQPoint: TypeAlias = Tuple[float, float]  # [re, im]  (len = 2)
IQMemory: TypeAlias = List[List[List[IQPoint]]]  # exp → shot → IQ points
HexMatrix = List[List[str]]


_KEY_DELIMITER = "~"
_HDF5_JOB_RESULTS_PATH_REGEX = re.compile(
    rf"experiments/(.*)/slot{_KEY_DELIMITER}(\d+)/measurement"
)
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
        readouts = _read_readouts_from_hdf5(hdf5_file)

    return QuantumJob(
        tuid=tuid,
        meas_return=meas_return,
        meas_level=meas_level,
        meas_return_cols=meas_return_cols,
        n_qubits=n_qubits,
        raw_results=raw_results,
        job_id=job_id,
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

        _save_readouts_to_hdf5(hdf5_file, readouts=job.readouts)
        _save_results_to_hdf5(hdf5_file, job.raw_results)


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


def get_experiment_name(circuit_name: str, expt_index: int):
    """
    Creates a cleaned version of a given experiment name

    Args:
        circuit_name: the name of the experiment's circuit
        expt_index: the index of the experiment in the list of the job's experiments

    Returns:
        a sanitized name to use internally
    """
    name = "".join(x for x in circuit_name if x.isalnum() or x in " -_,.()")
    return f"{name}{_KEY_DELIMITER}{expt_index}"


def _save_readouts_to_hdf5(file: h5py.File, readouts: List[CircuitReadout]) -> None:
    """Saves a gate-level job's readouts, in the order of its experiments, as JSON

    Args:
        file: the HDF5 file to save to
        readouts: the readouts of the job's experiments
    """
    group = file.require_group(_HDF5_READOUTS_PATH)
    group.attrs["readouts"] = json.dumps([readout.to_dict() for readout in readouts])


def _read_readouts_from_hdf5(file: h5py.File) -> List[CircuitReadout]:
    """Reads a gate-level job's readouts from the HDF5 file"""
    readouts = json.loads(file[_HDF5_READOUTS_PATH].attrs["readouts"])
    return [CircuitReadout.from_dict(readout) for readout in readouts]


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
    # come before "~2", pairing every experiment after the 9th with another's readout.
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
                f"unexpected ndarray shape {data.shape} for channel {channel_id}"
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
