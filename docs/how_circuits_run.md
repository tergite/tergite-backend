# How circuits run

What the backend does to a circuit, beyond the gates it names, that can change the counts or IQ points a job
returns. A change to any of these changes users' results, so it is recorded here and in the changelog.
`tests/services/test_schedule_snapshots.py` checks the schedules of random circuits against those the backend compiled
before its refactor of v2026.11, qubit by qubit.

## Each shot starts after 500 µs

Every shot begins with 500 µs in which nothing plays, so that the qubits relax from the last shot. The backend does
not reset them actively.

## Gates run as late as they can

Each gate runs as late as the gates after it on its own qubits let it: it ends as the next gate on any of its qubits
starts, and each qubit's last gate ends with the circuit. This is qiskit's ALAP scheduling, which the SDK used when it
sent pulse schedules. A qubit therefore idles least between its first gate and its measurement, whatever the other
qubits do meanwhile.

For example, in

```
rx(pi) $2;
reset $0;
c[0] = measure $0;
c[2] = measure $2;
```

the `rx` on qubit 2 runs right before its measurement, not before the 300 µs reset of qubit 0, in which it would
decay.

- Gates on different qubits run side by side wherever their order on each qubit allows it, so the order of a circuit's
  statements is kept on each qubit, not across them.
- A `barrier` lines its qubits up: no gate after it on one of them starts before all gates before it on any of them
  end. Use it where gates on different qubits must keep their order in time.
- A mid-circuit measurement runs as late as the gates after it on its qubit let it; with none after it, it ends with
  the circuit, with the final measurements.
- A pulse-level block (a `cal` block, or a gate with a `defcal`) holds the qubits its frames play on, both of a
  coupler's: what comes before it on them ends before it starts, and what comes after it on them starts once its frames
  are done. Gates on other qubits run beside it.
- A measurement holds its classical bit too, so two measurements into one bit keep their order.

## Times are on a 4 ns grid

The Qblox clusters play on a grid of 4 ns. A `delay` is rounded to the nearest 4 ns, and is dropped if that is 0. The
calibrated durations of the π pulse, the readout pulse, its acquisition and the acquisition's delay are rounded to the
nearest 4 ns too: an acquisition delay calibrated at 50 ns starts the acquisition 48 ns after the readout pulse.

## A reset waits

`reset` waits 300 µs for the qubit to relax; it plays nothing.

## rz is virtual

`rz`, and the other gates that decompose into it (`z`, `s`, `t`, `p`, `h`, …), shift the phase of the qubit's drive
for the pulses after it; nothing plays. Each takes one 4 ns grid interval, as a sequencer updates a phase at most once
per interval.

## Phases turn the other way on a drive whose LO sits below the qubit

On the clusters, a phase turns the other way on a qubit whose drive's local oscillator, in the hardware config, sits
below its frequency. The backend negates the angle of `rz`, the axis of a rotation (`ry`, `y`, `h`, …), and the
`shift_phase` and `set_phase` of a `cal` block on that qubit's drive frame, so that they turn as on any other qubit.
The `defcal`s the backend publishes are written for this, and play what their gates play.

A `cal` block's waveforms play as given on such a drive: a complex envelope's phase is not negated, so `shift_phase`
followed by `play` turns the other way from playing the envelope rotated by that phase.
