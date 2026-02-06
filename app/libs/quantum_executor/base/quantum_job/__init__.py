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

import re
from collections import namedtuple
from pathlib import Path
from typing import (
    Any,
    Callable,
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
from qiskit.qobj import PulseQobj, PulseQobjConfig, PulseQobjInstruction
from quantify_scheduler.enums import BinMode

from ...utils.general import search_nested
from .dtos import (
    ByteOrder,
    MeasLvl,
    MeasProtocol,
    MeasRet,
    NativeQobjConfig,
    QobjData,
    QobjHeaderMetadata,
    QobjMetadata,
    QobjSweepData,
    QuantumJob,
    SweepParamMetadata,
)
from .typing import (
    QChannel,
    QDataset,
    QExperimentName,
    QJobResult,
    RepetitionsByAcquisitionsMatrix,
)

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
        local = hdf5_file.attrs.get("local")
        raw_results = _extract_results_from_hdf5(hdf5_file)
        qobj_data = _read_hdf5_attributes(
            hdf5_file, path=_HDF5_QOBJ_DATA_PATH, type_=QobjData
        )
        metadata = _read_hdf5_attributes(
            hdf5_file, path=_HDF5_QOBJ_METADATA_PATH, type_=QobjMetadata
        )
        qobj = qobj_data.to_qobj()

    return QuantumJob(
        tuid=tuid,
        meas_return=meas_return,
        local=local,
        meas_level=meas_level,
        meas_return_cols=meas_return_cols,
        n_qubits=n_qubits,
        raw_results=raw_results,
        qobj_data=qobj_data,
        metadata=metadata,
        job_id=job_id,
        qobj=qobj,
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
        hdf5_file.attrs["local"] = job.local

        header_dict = job.qobj.header.to_dict()
        _save_qobj_header_to_hdf5(hdf5_file, header_dict=header_dict)
        _save_sweep_data_to_hdf5(hdf5_file, header_dict=header_dict)
        _save_qobj_to_hdf5(hdf5_file, job=job)
        _save_results_to_hdf5(hdf5_file, job.raw_results)


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


def _collect_from_dict(acq_instrs: Sequence[dict]) -> AcqParams:
    """Gather qubits / memory_slots from a sequence of `dict` instructions."""
    qubits: List[int] = []
    memory_slots: List[int] = []

    for inst in acq_instrs:
        try:
            q_list = inst["qubits"]
            m_list = inst["memory_slot"]
        except KeyError as err:
            raise ValueError(f"Acquire instruction dict is missing {err!s}") from None

        if len(q_list) != len(m_list):
            raise ValueError(
                "Acquire instruction dict has mismatched list lengths "
                f"(qubits={len(q_list)}, memory_slot={len(m_list)})."
            )

        qubits.extend(q_list)
        memory_slots.extend(m_list)

    return AcqParams(qubits=qubits, memory_slots=memory_slots)


def get_acquisition_parameters_from_experiment(
    exp_index: int,
    qobj: "PulseQobj",
    *,
    mode: Literal["object", "dict"] = "object",
) -> AcqParams:
    """
    Extract the qubit indices and memory-slot indices used by *acquire*
    instructions in a Pulse experiment.

    Args:
        exp_index: index of the experiment in `qobj.experiments`.
        qobj: a :class:`PulseQobj` containing the experiments.
        mode: `object` (default) - parse the instructions objects directly;
              `dict` - parse after converting the qobj to a dict.
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

    # check mode and parse appropriately
    if mode == "object":
        acq_instrs = [
            inst for inst in experiment_obj.instructions if inst.name == "acquire"
        ]
        if not acq_instrs:
            raise ValueError(
                f"Experiment {exp_index} contains no 'acquire' instruction."
            )
        return _collect_from_obj(acq_instrs)

    elif mode == "dict":
        qobj_dict = qobj.to_dict()
        experiment_dict = qobj_dict["experiments"][exp_index]
        acq_instrs = [
            inst
            for inst in experiment_dict["instructions"]
            if inst["name"] == "acquire"
        ]
        if not acq_instrs:
            raise ValueError(
                f"Experiment {exp_index} contains no 'acquire' instruction."
            )
        return _collect_from_dict(acq_instrs)

    return AcqParams(qubits=qubits, memory_slot=memory_slot)


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


def discriminate_results(
    job: QuantumJob,
    discriminator: Callable[[int, npt.NDArray[np.complexfloating]], int],
    *,
    num_of_states: Union[Literal[2], Literal[3]] = 2,
    byteorder: ByteOrder = ByteOrder.LITTLE_ENDIAN,
    **kwargs,
) -> HexMatrix:
    """
    Convert raw IQ data in `job.raw_results` into hexadecimal read-outs.

    Steps:
    1. Extract `acquire` mappings (qubit -> classical slot once per experiment)
    2. For every acquisition channel, run the supplied `discriminator` to obtain discriminated 0/1/2 int per shot
    3. Pack the values into a register-shaped array (`full_register-length` x `shots`), respecting qubit->slot map
    4. Convert each register state (row) to a hex string

    Args:
        job: quantum job whose results are to be discriminated
        discriminator: a function which takes two arguments "qubit_index" and "iq_points" (an array)
            and returns a binary value (0/1).
        num_of_states: the number of states the discriminator produces; default=2
        byteorder: the byte order of the acquisition channel list; default=ByteOrder.LITTLE_ENDIAN

    Returns:
        HexMatrix `results[experiments][shot]` -> hex string
        Nested list of the hex (base 16) representations of the states e.g. 0, 1.
        Each inner list corresponds to one experiment.
        Each item in the inner list corresponds to a shot number
    """
    if not callable(discriminator):
        raise ValueError("'discriminator' must be callable")

    qobj = job.qobj
    full_register_length: int = job.n_qubits
    out: HexMatrix = []

    # loop over experiments, where each experiment is produced by one circuit
    for exp_index, expt_dataset in enumerate(job.raw_results.values()):

        exp_obj = qobj.experiments[exp_index]
        header = getattr(exp_obj, "header", None)
        reg_len = getattr(header, "memory_slots", None)
        acq = get_acquisition_parameters_from_experiment(exp_index=exp_index, qobj=qobj)

        if reg_len is None:
            print(qobj)
            reg_len = getattr(qobj.config, "memory_slots", None)
        if reg_len is None:
            reg_len = max(acq.memory_slots) + 1
        reg_len = int(reg_len)

        # map qubit -> classical slot
        # get acquisition instruction params
        meas_map = dict(zip(acq.qubits, acq.memory_slots))
        slot_to_qubit = dict(zip(acq.memory_slots, acq.qubits))

        no_of_repetitions: int = expt_dataset.sizes["repetition"]
        register = np.zeros((reg_len, no_of_repetitions), dtype=np.int8)

        # process each acquisition
        for channel, acquisitions in expt_dataset.items():
            # there shouldn't be more than one measurement data per channel
            if acquisitions.shape[1] != 1:
                raise ValueError(
                    f"Experiment data contain more than one measurement per channel."
                    f"Instead {acquisitions.shape[1]} measurements found for qubit channel {channel}"
                )
            slot = int(channel)

            if slot not in slot_to_qubit:
                continue

            qubit_idx = slot_to_qubit[slot]

            iq_values: npt.NDArray[np.complexfloating] = acquisitions.data[:, 0]
            disc_res = discriminator(qubit_idx, iq_values)

            if slot >= reg_len:
                raise ValueError(
                    f"Acquire maps qubit {qubit_idx} to memory_slot {slot}, "
                    f"but reg_len is {reg_len}. (Circuit/Qobj mismatch)"
                )

            # support scalar and vector output
            if np.isscalar(disc_res):
                register[slot, :] = disc_res
            else:
                if len(disc_res) != no_of_repetitions:
                    raise ValueError(
                        f"Discriminator for qubit {qubit_idx} "
                        f"returned {len(disc_res)} values, expected {no_of_repetitions}"
                    )
                register[slot, :] = disc_res

        # convert to hex per repetition
        bitarrays_per_rep = register.transpose()
        base_10_per_rep = _bitarrays_to_decimal(
            bitarrays_per_rep, base=num_of_states, byteorder=byteorder
        )
        hex_per_rep = _dec_to_hex(base_10_per_rep)
        out.append(hex_per_rep.tolist())

    return out


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
    """
    if job.raw_results is None:
        raise ValueError(
            "xarray_to_list: Can't find Quantum job experiment results `job.raw_results`."
        )

    qobj = job.qobj
    experiments_mem: IQMemory = []

    # sort experiments by name
    for expt_name, dataset in sorted(job.raw_results.items(), key=lambda kv: kv[0]):
        if not isinstance(dataset, xr.Dataset):
            raise TypeError(
                "xarray_to_list: expected an xarray.Dataset in raw_results values"
            )

        # get qobj experiment index first
        exp_index = _parse_exp_index_from_name(expt_name, delimiter="~")
        if exp_index is None:
            # for debugging purposes keep this, otherwise raise ValueError
            exp_index = len(experiments_mem)

        # get acquisition mapping from qobj
        acq = get_acquisition_parameters_from_experiment(
            exp_index=exp_index, qobj=qobj, mode="object"
        )
        meas_map: Dict[int, int] = dict(zip(acq.qubits, acq.memory_slots))

        # invert: memory_slot -> qubit
        slot_to_qubit: Dict[int, int] = {}
        for q, s in meas_map.items():
            if s in slot_to_qubit:
                raise ValueError(
                    f"xarray_to_list: multiple qubits map to the same memory_slot={s} "
                    f"(qubits {slot_to_qubit[s]} and {q})."
                )
            slot_to_qubit[s] = q

        # slot order defines output order (clbit order)
        slot_order = sorted(slot_to_qubit.keys())

        # Number of shots / repetitions (may be 1 for averaged data)
        number_of_repeatitions = dataset.sizes.get("repetition", 1)

        exp_mem: List[List[IQPoint]] = []
        for rep_idx in range(number_of_repeatitions):
            repeatition_vals: List[IQPoint] = []
            for slot in slot_order:
                q = slot_to_qubit[slot]

                key_slot = str(slot)

                if key_slot not in dataset.data_vars:
                    raise KeyError(
                        f"xarray_to_list: cannot find dataset variable for qubit={q} or slot={slot}. "
                        f"Available keys: {list(dataset.data_vars.keys())}"
                    )

                # get numpy view
                arr = dataset[key_slot].data
                # slice the repetition dimension
                # (repetition, acq_index_N)
                if arr.ndim == 2:
                    row = arr[rep_idx, ...]
                # averaged data, no repetition dim
                elif arr.ndim == 1:
                    row = arr
                else:
                    raise ValueError(
                        f"xarray_to_list: unexpected ndarray shape {arr.shape}"
                    )

                # Flatten any remaining acquisition-index dimensions
                for val in np.ravel(row):
                    c = complex(val.item())  # ensure Python complex
                    repeatition_vals.append((float(c.real), float(c.imag)))

            exp_mem.append(repeatition_vals)

        experiments_mem.append(exp_mem)

    return experiments_mem


def to_native_qobj_config(config: PulseQobjConfig) -> "NativeQobjConfig":
    """Converts the pulse qobj config to native qobj config

    Args:
        config: the configuration object of the pulse qobj

    Returns:
        NativeQobjConfig instance
    """
    bin_mode = _get_bin_mode(config)
    protocol = _get_meas_protocol(config)
    n_qubits = config.n_qubits
    meas_level = MeasLvl(config.meas_level)

    if bin_mode is BinMode.AVERAGE and protocol is MeasProtocol.SSB_INTEGRATION_COMPLEX:
        return NativeQobjConfig(
            acq_return_type=complex,
            protocol=protocol,
            bin_mode=bin_mode,
            meas_level=meas_level,
            meas_return=MeasRet.AVERAGED,
            meas_return_cols=1,
            n_qubits=n_qubits,
            shots=config.shots,
        )

    if bin_mode is BinMode.AVERAGE and protocol is MeasProtocol.TRACE:
        return NativeQobjConfig(
            acq_return_type=np.ndarray,
            protocol=protocol,
            bin_mode=bin_mode,
            meas_level=meas_level,
            meas_return=MeasRet.AVERAGED,
            meas_return_cols=16384,  # length of a trace
            n_qubits=n_qubits,
            shots=config.shots,
        )

    if bin_mode is BinMode.APPEND and protocol is MeasProtocol.SSB_INTEGRATION_COMPLEX:
        return NativeQobjConfig(
            acq_return_type=np.ndarray,
            protocol=MeasProtocol.SSB_INTEGRATION_COMPLEX,
            bin_mode=BinMode.APPEND,
            meas_level=meas_level,
            meas_return=MeasRet.APPENDED,
            meas_return_cols=config.shots,
            n_qubits=n_qubits,
            shots=config.shots,
        )

    raise RuntimeError(
        f"Combination {(config.meas_return, config.meas_return)} is not supported."
    )


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
    # save header backend metadata
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
    # save the raw metadata
    if isinstance(job.metadata, QobjMetadata):
        _save_hdf5_attributes(
            file, path=_HDF5_QOBJ_METADATA_PATH, source=job.metadata.to_dict()
        )

    # save the raw data
    if isinstance(job.qobj_data, QobjData):
        _save_hdf5_attributes(
            file, path=_HDF5_QOBJ_DATA_PATH, source=job.qobj_data.to_dict()
        )


def _save_sweep_data_to_hdf5(file: h5py.File, header_dict: dict):
    """Saves the sweep data and metadata to the HDF5 file

    Args:
        file: the HDF5 file to save to
        header_dict: the dict from QobjHeader
    """
    try:
        sweep_data = QobjSweepData.from_qobj_header(header_dict)
    except ValueError:
        # return early if no sweep data
        return

    # save header sweep metadata
    _save_hdf5_attributes(file, path=_HDF5_SWEEP_DATA_PATH, source=sweep_data.metadata)

    # save the raw sweep data
    sweep_data_dict = sweep_data.model_dump()
    for path_segments in search_nested(sweep_data_dict, "slots"):
        sweep_group = file.require_group(_HDF5_SWEEP_DATA_PATH)
        slots_path = "/".join(path_segments)
        slots_group = sweep_group.create_group(slots_path)

        # save param metadata
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

        hdf5_dataset: h5py.Dataset = file[path]
        xarray_dataset[acq] = (["repetition", f"acq_index_{acq}"], hdf5_dataset[:])

    return results


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
        # do nothing if dict is empty
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


def _get_bin_mode(qobj_conf: PulseQobjConfig) -> BinMode:
    """Gets the BinMode based on the meas_return of the qobj.config

    Args:
        qobj_conf: the qobject config whose bin mode is to be obtained

    Returns:
        the BinMode for the given qobj
    """
    meas_return = qobj_conf.meas_return
    if isinstance(meas_return, int):
        return {
            int(MeasRet.APPENDED): BinMode.APPEND,
            int(MeasRet.AVERAGED): BinMode.AVERAGE,
        }[meas_return]

    # FIXME: For some reason, PulseQobjConfig expects to be an int
    #   yet our fixtures all have strings.
    meas_return = str.lower(qobj_conf.meas_return)
    return {
        "avg": BinMode.AVERAGE,
        "average": BinMode.AVERAGE,
        "averaged": BinMode.AVERAGE,
        "single": BinMode.APPEND,
        "append": BinMode.APPEND,
        "appended": BinMode.APPEND,
    }[meas_return]


def _get_meas_protocol(qobj_conf: PulseQobjConfig) -> "MeasProtocol":
    """Gets the measurement protocol for the given qobject

    Args:
        qobj_conf: the qobject config from which to extract the measurement protocol

    Returns:
        the measurement protocol for the given qobject
    """
    return {
        0: MeasProtocol.TRACE,
        1: MeasProtocol.SSB_INTEGRATION_COMPLEX,
        2: MeasProtocol.SSB_INTEGRATION_COMPLEX,
    }[qobj_conf.meas_level]


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
