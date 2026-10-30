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
"""Tests for the pure steps of building a quantify schedule"""
import pytest

pytest.importorskip("quantify_scheduler")

from libs.quantum_executor.quantify.channel import QuantifyChannelRegistry
from libs.quantum_executor.quantify.experiment import (
    PLACEHOLDER_CLOCK_FREQ_HZ,
    _align_to_barrier,
    _get_channel_plans,
    _get_initial_clock_frequencies,
    _group_by_slot,
    _State,
    _to_tick,
)
from libs.quantum_executor.quantify.instruction import (
    DelayInstruction,
    SetFreqInstruction,
)

_INTERVAL = 4e-9


@pytest.mark.parametrize(
    "t, expected_tick", [(0.0, 0), (5.9e-9, 1), (6e-9, 2), (8.1e-9, 2)]
)
def test_time_rounds_to_the_nearest_tick(t, expected_tick):
    assert _to_tick(t, _INTERVAL) == expected_tick


@pytest.mark.parametrize("should_include_frequency_ops", [False, True])
def test_frequency_instructions_are_scheduled_only_when_included(
    should_include_frequency_ops,
):
    registry = QuantifyChannelRegistry()
    set_freq = _add_set_freq(registry, "q0.01", t0=0.0, frequency=4e9)
    delay = _add_delay(registry, "q0.01", t0=8e-9)

    [plan] = _get_channel_plans(registry, should_include_frequency_ops)

    expected = [set_freq, delay] if should_include_frequency_ops else [delay]
    assert (plan.clock, plan.schedulable, plan.is_empty) == ("q0.01", expected, False)


def test_a_clock_starts_at_its_earliest_set_frequency():
    registry = QuantifyChannelRegistry()
    _add_set_freq(registry, "q0.01", t0=8e-9, frequency=5e9)
    _add_set_freq(registry, "q0.01", t0=0.0, frequency=4e9)
    _add_delay(registry, "q1.01", t0=0.0)
    _add_set_freq(registry, "cl0.baseband", t0=0.0, frequency=6e9)

    frequencies = _get_initial_clock_frequencies(registry)

    assert frequencies == {
        "q0.01": 4e9,
        "q1.01": PLACEHOLDER_CLOCK_FREQ_HZ,
        "cl0.baseband": 0.0,
    }


def test_instructions_group_by_the_tick_they_start_at():
    registry = QuantifyChannelRegistry()
    first = _add_delay(registry, "q0.01", t0=0.0)
    second = _add_delay(registry, "q0.01", t0=8e-9)
    third = _add_delay(registry, "q0.01", t0=8.1e-9)
    plans = _get_channel_plans(registry, should_include_frequency_ops=False)

    by_clock_slot = _group_by_slot(plans, _INTERVAL)

    assert by_clock_slot == {"q0.01": {0: [first], 2: [second, third]}}


def test_active_clocks_wait_for_the_latest_to_be_ready():
    states = {
        "busy": _State(
            dispatch_tick=3, sync_tick=1, last_label="", is_any_emitted=True
        ),
        "idle": _State(
            dispatch_tick=0, sync_tick=0, last_label="", is_any_emitted=False
        ),
        "done": _State(
            dispatch_tick=0, sync_tick=0, last_label="", is_any_emitted=False
        ),
    }

    _align_to_barrier(states, slot=2, active_clocks=["busy", "idle"])

    assert {clock: state.sync_tick for clock, state in states.items()} == {
        "busy": 1,
        "idle": 4,
        "done": 0,
    }


def _add_set_freq(
    registry: QuantifyChannelRegistry, clock: str, t0: float, frequency: float
) -> SetFreqInstruction:
    instruction = SetFreqInstruction(
        t0=t0, channel=registry[clock], duration=0.0, frequency=frequency
    )
    registry.add(instruction)
    return instruction


def _add_delay(
    registry: QuantifyChannelRegistry, clock: str, t0: float
) -> DelayInstruction:
    instruction = DelayInstruction(t0=t0, channel=registry[clock], duration=_INTERVAL)
    registry.add(instruction)
    return instruction
