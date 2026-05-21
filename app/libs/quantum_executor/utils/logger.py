# This code is part of Tergite
#
# (C) Axel Andersson (2022)
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
# Refactored by Martin Ahindura (2024)
# Refactored by Chalmers Next Labs 2025

import logging
import logging.handlers
from os.path import abspath, join
from pathlib import Path
from typing import Union

import pandas as pd
import tabulate

from app.utils.compat import (
    TUID,
    CompiledSchedule,
    Schedule,
    locate_experiment_container,
)

from .general import load_config, search_nested


class Line:
    __slots__ = "idx", "raw"

    def __init__(self: object, idx: int, raw: str):
        self.idx = idx
        self.raw = raw

    def __str__(self: "Line") -> str:
        s = self.raw.replace(" " * 2, "\t")
        return f"{self.idx}:\t{s}".expandtabs(16)


class ExperimentLogger:
    def __init__(
        self,
        tuid: Union[str, "TUID"],
        /,
        *,
        formatter=logging.Formatter("%(asctime)s ▪ %(levelname)s ▪ %(message)s"),
    ):
        """Initialize the ExperimentLogger for the ongoing experiment."""
        self.tuid = tuid
        self.formatter = formatter
        self.folder = abspath(locate_experiment_container(self.tuid))
        self.logger = self.make_logger(__name__, self.folder, self.formatter)
        self.logger.propagate = False

        # --- Default state
        self.last_timing_table = pd.DataFrame()
        self.last_programs = dict()

    def warning(self, message, /, **kwargs):
        """Log a message at the warning level."""
        self.logger.warning(message, **kwargs)

    def error(self, message, /, **kwargs):
        """Log a message at the error level."""
        self.logger.error(message, **kwargs)

    def info(self, message, /, **kwargs):
        """Log a message at the info level."""
        self.logger.info(message, **kwargs)

    @staticmethod
    def make_logger(module_name, output_folder, formatter, /):
        """Create a logging instance for the Python interpreter which is using QuantumExecutor.
        Module name is the name of the logging module, usually __file__.
        The output folder is the storage location for the data of the experiment.
        The logging formatter specifies how messages appear in the log, the default formatter is:
            "%(asctime)s ▪ %(levelname)s ▪ %(message)s"
        Log files have the name "log.txt" and are stored in the output folder.
        """
        logger = logging.getLogger(module_name)
        logger.setLevel(logging.INFO)
        file_handler = logging.FileHandler(
            join(output_folder, "log.txt"), encoding="utf-8"
        )
        file_handler.setFormatter(formatter)

        if logger.hasHandlers():
            logger.handlers.clear()

        logger.addHandler(file_handler)
        return logger

    @staticmethod
    def clean_Q1ASM_program(program_lines: list, /) -> set:
        """Clean a single Q1ASM program given as lines of code in string format.
        Returns a set of enumerated lines, where argument, function & comment
        are separated by exactly two whitespaces.
        """
        # remove empty lines
        program_lines = list(filter(lambda l: l.replace(" ", "") != "", program_lines))
        # replace tabs with spaces
        program_lines = list(map(lambda l: l.replace("\t", " " * 3), program_lines))
        # removing both the leading and the trailing whitespace
        program_lines = list(map(lambda l: str.strip(l), program_lines))

        program_set = set()
        # add line enumeration
        for line_idx, line in enumerate(program_lines):
            # reduce whitespace
            while line != (line := line.replace(" " * 3, " " * 2)):
                continue

            program_set.add((line_idx, line))

        return program_set

    def log_schedule(self, schedule: Union["Schedule", "CompiledSchedule"], /):
        """Log the timing table for a Quantify schedule.
        A delta is computed with the last executed timing table and only the changed operations
        are saving in the log. When the current schedule has changed operations, the entire current schedule
        is logged. If the previous and the current schedules are identical, nothing is logged.
        """
        # catch deprecation warnings, let Quantify fix those
        # with warnings.catch_warnings(record=True) as _:
        df = schedule.timing_table.data

        df.sort_values("abs_time", inplace=True)
        df.drop(["waveform_op_id"], axis=1, inplace=True)
        # df["operation"] = df["operation_hash"].map(lambda op: schedule.operations[op].name)

        delta = pd.concat([self.last_timing_table, df]).drop_duplicates(keep=False)
        self.last_timing_table = df

        if not delta.empty:
            tt = tabulate.tabulate(df.values, df.columns, tablefmt="simple")
            tt = "\n" + str(tt)
            self.info(f"Timing table: {tt}")

        out_dir = Path(self.folder)
        out_dir.mkdir(parents=True, exist_ok=True)
        csv_path = out_dir / "compiled_schedule_expected.csv"

        # Keep only stable columns
        cols = [
            c
            for c in [
                "port",
                "clock",
                "abs_time",
                "duration",
                "is_acquisition",
                "operation",
            ]
            if c in df.columns
        ]

        df[cols].to_csv(csv_path, index=False)
        self.info(f"Wrote timing table CSV to {csv_path}")

    @staticmethod
    def format_Q1ASM(path: tuple, code: set) -> str:
        """Takes a clean program set for a specific sequencer and formats it nicely
        to make the logfiles more human-readable.
        """
        fstr = ""
        path = path[:-1]  # suppress "seq_fn" to save some space when logging
        for i, k in enumerate(path):
            fstr += k
            if i < (len(path) - 1):
                fstr += ("\n" + "\t" * i + "┗━━━━ ").expandtabs()
            else:
                fstr += ":\n"
        loc = list(code)
        loc = sorted(loc, key=lambda item: item[0])
        loc = [Line(idx, line) for idx, line in loc]
        fstr += "\n".join(map(str, loc))
        fstr += "\n"
        return fstr

    def log_Q1ASM_programs(self, compiled_schedule: "CompiledSchedule", /):
        """Log the Q1ASM programs of all sequencers of all instruments of all instruments
        as specified by a given compiled Quantify schedule.
        A delta is computed with the last compiled schedule and only the changed sequencing
        is saved in the log. When a sequencer has updated programming, both the previous and
        the current Q1ASM programming for that sequencer is printed (symmetric difference).
        """

        programs = dict()
        # extract sequencer programs by searching for "seq_fn"
        for path in search_nested(compiled_schedule.compiled_instructions, "seq_fn"):
            # Get the parent dictionary that contains both "seq_fn" and "sequence"
            parent = compiled_schedule.compiled_instructions
            for key in path[:-1]:
                parent = parent[key]
            seq_fn_value = parent.get("seq_fn")
            if seq_fn_value is None:
                # If no file path is provided, try to use the embedded sequence
                sequence_dict = parent.get("sequence")
                if sequence_dict and "program" in sequence_dict:
                    program = sequence_dict["program"]
                else:
                    self.info(
                        f"Skipping path {path} because neither 'seq_fn' nor a valid 'sequence' is provided."
                    )
                    continue
            else:
                # Old behavior: load the configuration file using seq_fn_value
                try:
                    config = load_config(seq_fn_value)
                except Exception as e:
                    self.error(
                        f"Error loading config from {seq_fn_value} for path {path}: {e}"
                    )
                    continue
                if "program" not in config:
                    self.warning(
                        f"Config file {seq_fn_value} for path {path} does not contain a 'program'."
                    )
                    continue
                program = config["program"]

            # Process the program lines and clean them up
            program_lines = program.split("\n")
            program_set = self.clean_Q1ASM_program(program_lines)
            programs[path] = program_set

        # Compute delta with last compiled schedule and log changes
        for sequencer, program in programs.items():
            if sequencer not in self.last_programs:
                self.info(
                    f"+{len(program)} lines. Q1ASM:\n+++ {self.format_Q1ASM(sequencer, program)}"
                )
            else:
                delta = programs[sequencer] ^ self.last_programs[sequencer]
                removed_loc = set(
                    filter(lambda line: line in self.last_programs[sequencer], delta)
                )
                added_loc = set(filter(lambda line: line in programs[sequencer], delta))
                if removed_loc:
                    self.info(
                        f"-{len(removed_loc)} lines. Q1ASM:\n--- {self.format_Q1ASM(sequencer, removed_loc)}"
                    )
                if added_loc:
                    self.info(
                        f"+{len(added_loc)} lines. Q1ASM:\n+++ {self.format_Q1ASM(sequencer, added_loc)}"
                    )

        self.last_programs = programs
