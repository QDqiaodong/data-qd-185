"""Soak-log endurance record analysis (schema v7).

A soak record never stores a per-cycle vector.  Instead it stores holds:
input pin values that stay constant for a run of clock ticks.  Probes
sample declared outputs on their own clock grids; each probe trips its
protection after ``trip_after`` consecutive mismatching samples, and one
matching sample clears that probe's own losing streak.

The record length N may reach 10**12 clocks, so the full timeline is never
materialized.  For every hold the circuit (at most 8 flip-flops, hence at
most 3**8 = 6561 states) is walked until the state repeats; the resulting
orbit lets every clock's pre-edge state/output be answered by arithmetic,
and probe samples (also up to 10**12 per probe) are reconciled in periodic
blocks instead of one iteration per sample.  The analysis is still one
uninterrupted run from the netlist initial values, with no reset at window
or segment boundaries; the frozen waveform is cut out of that same run.
"""
from bisect import bisect_right
from math import gcd as _gcd

from .gates import evaluate
from .io import fields, integer, label, rows
from .netlist import BITS, CONSTANTS, compile_circuit

SCHEMA_VERSION = 7
MAX_FLOPS = 8
MAX_HOLDS = 64
MAX_MONITORS = 16
MAX_CAPTURE = 64
MAX_N = 10**12


class _Engine:
    """One-cycle transition for fixed input assignments, cached per hold."""

    def __init__(self, inputs, gates, flops, outputs, order):
        self.inputs = inputs
        self.gates = gates
        self.flops = flops
        self.outputs = outputs
        self.order = order
        self.flop_names = sorted(flops)
        self.initial = tuple(flops[name]["initial"] for name in self.flop_names)
        self.cache = {}

    def step(self, assignment, state):
        key = (assignment, state)
        cached = self.cache.get(key)
        if cached is None:
            values = dict(CONSTANTS)
            values.update(zip(self.inputs, assignment))
            values.update(zip(self.flop_names, state))
            for name in self.order:
                gate = self.gates[name]
                values[name] = evaluate(gate["op"], [values[arg] for arg in gate["args"]])
            observed = tuple(values[name] for name in self.outputs)
            next_state = tuple(values[self.flops[name]["d"]] for name in self.flop_names)
            cached = (observed, next_state)
            self.cache[key] = cached
        return cached


def compile_soak(data):
    """Validate the whole endurance record before any clock is analyzed."""
    fields(data, ("circuit", "holds", "monitors", "capture"))
    circuit = data["circuit"]
    fields(circuit, ("inputs", "gates", "flops", "outputs"))
    inputs, gates, flops, outputs, order = compile_circuit(circuit)
    if len(flops) > MAX_FLOPS:
        raise ValueError("soak record allows at most 8 flops")

    hold_rows = rows(data["holds"], "holds", 1, MAX_HOLDS)
    holds = []
    hold_ids = set()
    total = 0
    for row in hold_rows:
        fields(row, ("id", "ticks", "inputs"))
        hold_id = label(row["id"], "hold.id")
        if hold_id in hold_ids:
            raise ValueError(f"duplicate hold ID: {hold_id}")
        hold_ids.add(hold_id)
        ticks = integer(row["ticks"], "hold.ticks", 1, MAX_N)
        total += ticks
        if total > MAX_N:
            raise ValueError("total hold ticks exceed 10**12")
        given = row["inputs"]
        if not isinstance(given, dict):
            raise ValueError("hold.inputs must be an object")
        if set(given) != set(inputs):
            raise ValueError("hold.inputs must give every declared input exactly once")
        assignment = tuple(bit(given[name]) for name in inputs)
        holds.append({"id": hold_id, "ticks": ticks, "assignment": assignment})

    monitor_rows = rows(data["monitors"], "monitors", 1, MAX_MONITORS)
    monitors = []
    monitor_ids = set()
    for row in monitor_rows:
        fields(row, ("id", "output", "first", "through", "stride",
                     "expected", "trip_after"))
        monitor_id = label(row["id"], "monitor.id")
        if monitor_id in monitor_ids:
            raise ValueError(f"duplicate monitor ID: {monitor_id}")
        monitor_ids.add(monitor_id)
        if not isinstance(row["output"], str) or row["output"] not in outputs:
            raise ValueError("monitor.output must reference a declared output")
        first = integer(row["first"], "monitor.first", 0, MAX_N - 1)
        through = integer(row["through"], "monitor.through", 0, MAX_N - 1)
        if through >= total:
            raise ValueError("monitor.through must be smaller than the record length N")
        if first > through:
            raise ValueError("monitor.through must be >= monitor.first")
        stride = integer(row["stride"], "monitor.stride", 1, MAX_N)
        trip_after = integer(row["trip_after"], "monitor.trip_after", 1, MAX_N)
        expected = bit(row["expected"])
        monitors.append({"id": monitor_id, "output": row["output"], "first": first,
                         "through": through, "stride": stride, "expected": expected,
                         "trip_after": trip_after})

    capture = data["capture"]
    fields(capture, ("before", "after"))
    before = integer(capture["before"], "capture.before", 0, MAX_CAPTURE)
    after = integer(capture["after"], "capture.after", 0, MAX_CAPTURE)
    return {"inputs": inputs, "gates": gates, "flops": flops, "outputs": outputs,
            "order": order, "holds": holds, "monitors": monitors,
            "capture": {"before": before, "after": after}, "total": total}


def bit(value):
    if not isinstance(value, str) or value not in BITS:
        raise ValueError('logic value must be string "0", "1" or "X"')
    return value


def build_orbit(engine, assignment, entry, ticks):
    """Walk one hold from its entry state until the state repeats or ticks end.

    Returns an orbit whose ``states``/``observed``/``next_states`` rows are
    aligned per visited offset; ``states[start:]`` repeats forever with the
    given period, and ``states_at`` maps a state back to its first offset.
    Holds longer than the orbit are answered by modular indexing, including
    the state that enters the following hold.
    """
    states, observed, next_states = [], [], []
    position = {}
    state = entry
    for _ in range(ticks):
        if state in position:
            # Repeated state: the edge already recorded at the first visit
            # produces the same next state, so the orbit closes here.
            return {"states": states, "observed": observed, "next_states": next_states,
                    "start": position[state], "states_at": position}
        position[state] = len(states)
        observed_now, next_state = engine.step(assignment, state)
        states.append(state)
        observed.append(observed_now)
        next_states.append(next_state)
        state = next_state
    return {"states": states, "observed": observed, "next_states": next_states,
            "start": len(states), "states_at": position}


def state_at(orbit, offset):
    """Pre-edge state at a zero-based offset inside one hold."""
    start = orbit["start"]
    if offset < start:
        return orbit["states"][offset]
    period = len(orbit["states"]) - start
    return orbit["states"][start + (offset - start) % period]


def observed_at(orbit, offset, output_index):
    state = state_at(orbit, offset)
    return orbit["observed"][orbit["states_at"][state]][output_index]


class _Probe:
    """Per-probe counters and protection state across the whole record."""

    def __init__(self, monitor, output_index):
        self.monitor = monitor
        self.output_index = output_index
        self.samples = 0
        self.mismatches = 0
        self.streak = 0
        self.streak_start_index = None
        self.streak_samples = 0
        self.trip_clock = None
        self.trip_streak_start = None
        self.triggered = False

    def consume(self, match, clock, sample_index):
        if match:
            self.streak = 0
            self.streak_start_index = None
            self.streak_samples = 0
            return
        self.mismatches += 1
        if self.streak == 0:
            self.streak_start_index = sample_index
        self.streak += 1
        self.streak_samples += 1
        if not self.triggered and self.streak >= self.monitor["trip_after"]:
            self.triggered = True
            self.trip_clock = clock
            self.trip_streak_start = sample_index - (self.streak_samples - 1)


def analyze_monitor(engine, probe, segments):
    """Reconcile every sample of one probe without iterating all N clocks."""
    monitor = probe.monitor
    first, through, stride = monitor["first"], monitor["through"], monitor["stride"]
    for segment in segments:
        base, ticks, orbit = segment["base"], segment["ticks"], segment["orbit"]
        if base > through or base + ticks - 1 < first:
            continue
        lo = max(first, base)
        hi = min(through, base + ticks - 1)
        j_lo, rem = divmod(lo - first, stride)
        if rem:
            j_lo += 1
        j_hi = (hi - first) // stride
        if j_lo > j_hi:
            continue
        total_samples = j_hi - j_lo + 1
        k_lo = first + j_lo * stride - base
        k_hi = first + j_hi * stride - base
        start = orbit["start"]
        # Samples landing in the non-repeating transient are few (<= 6560).
        prefix_count = 0
        if k_lo < start:
            prefix_count = (min(k_hi, start - 1) - k_lo) // stride + 1
        index = probe.samples
        for _ in range(prefix_count):
            actual = observed_at(orbit, k_lo, probe.output_index)
            probe.consume(actual == monitor["expected"], base + k_lo, index)
            index += 1
            k_lo += stride
        if prefix_count == total_samples:
            probe.samples = index
            continue
        sample_count = total_samples - prefix_count
        period = len(orbit["states"]) - start
        # Offsets visited under the stride repeat with this residue cycle.
        cycle_length = period // _gcd(stride, period)
        pattern = [observed_at(orbit, start + (k_lo - start + j * stride) % period,
                               probe.output_index)
                   for j in range(cycle_length)]
        full_cycles, tail = divmod(sample_count, cycle_length)
        mismatch_per_cycle = sum(value != monitor["expected"] for value in pattern)
        if all(value != monitor["expected"] for value in pattern):
            # Every remaining sample mismatches, so they form (or extend) one
            # unbroken streak; all bookkeeping is then O(1) arithmetic.
            open_streak = probe.streak_samples
            streak_start = probe.streak_start_index
            if probe.streak == 0:
                streak_start = index
            if not probe.triggered and open_streak + sample_count >= monitor["trip_after"]:
                probe.triggered = True
                # need-th sample of this block (1-based) reaches the threshold.
                need = monitor["trip_after"] - open_streak
                probe.trip_clock = base + k_lo + (need - 1) * stride
                probe.trip_streak_start = streak_start
            probe.mismatches += sample_count
            probe.streak += sample_count
            probe.streak_samples += sample_count
            probe.streak_start_index = streak_start
            index += sample_count
            probe.samples = index
            continue
        # The pattern contains at least one match, so a streak cannot survive
        # a full cycle: two explicit cycles without a trigger guarantee the
        # probe never trips inside this hold.  Simulation still stops early as
        # soon as the trigger fires; the bound is 2 * 3**8 explicit steps.
        safe_cycles = min(full_cycles, 2)
        settled_cycles = 0
        while settled_cycles < safe_cycles and not probe.triggered:
            for _ in range(cycle_length):
                actual = observed_at(orbit, k_lo, probe.output_index)
                probe.consume(actual == monitor["expected"], base + k_lo, index)
                index += 1
                k_lo += stride
            settled_cycles += 1
        remaining_cycles = full_cycles - settled_cycles
        if remaining_cycles and not probe.triggered:
            # Two full cycles saw a match, so every later cycle matches too;
            # only the counters need arithmetic.
            probe.mismatches += mismatch_per_cycle * remaining_cycles
            index += remaining_cycles * cycle_length
            k_lo += remaining_cycles * cycle_length * stride
            probe.streak = 0
            probe.streak_samples = 0
            probe.streak_start_index = None
        elif remaining_cycles:
            # Already triggered: the trip is fixed forever, so bulk-count the
            # remaining full cycles instead of walking each sample.
            probe.mismatches += mismatch_per_cycle * remaining_cycles
            index += remaining_cycles * cycle_length
            k_lo += remaining_cycles * cycle_length * stride
        # The partial final cycle is always short (<= 3**8 samples).
        for _ in range(tail):
            actual = observed_at(orbit, k_lo, probe.output_index)
            probe.consume(actual == monitor["expected"], base + k_lo, index)
            index += 1
            k_lo += stride
        probe.samples = index


def run_soak(data):
    spec = compile_soak(data)
    engine = _Engine(spec["inputs"], spec["gates"], spec["flops"],
                     spec["outputs"], spec["order"])

    # One uninterrupted run from netlist initial values: build each hold's
    # orbit from the state the previous hold actually ended in.
    state = engine.initial
    segments = []
    base = 0
    for hold in spec["holds"]:
        engine.cache = {}
        orbit = build_orbit(engine, hold["assignment"], state, hold["ticks"])
        segments.append({"id": hold["id"], "base": base, "ticks": hold["ticks"],
                         "assignment": hold["assignment"], "orbit": orbit})
        last_state = state_at(orbit, hold["ticks"] - 1)
        state = orbit["next_states"][orbit["states_at"][last_state]]
        base += hold["ticks"]
    final_state = state

    probes = []
    for monitor in spec["monitors"]:
        output_index = spec["outputs"].index(monitor["output"])
        probe = _Probe(monitor, output_index)
        analyze_monitor(engine, probe, segments)
        probes.append(probe)

    monitor_reports = []
    earliest = None
    for probe in probes:
        monitor = probe.monitor
        if probe.triggered:
            trip_clock = probe.trip_clock
            trip_start_clock = monitor["first"] + probe.trip_streak_start * monitor["stride"]
            if earliest is None or trip_clock < earliest:
                earliest = trip_clock
        else:
            trip_clock = None
            trip_start_clock = None
        monitor_reports.append({
            "id": monitor["id"],
            "output": monitor["output"],
            "samples": probe.samples,
            "mismatches": probe.mismatches,
            "trip_clock": trip_clock,
            "streak_start_clock": trip_start_clock,
        })

    trigger_ids = None
    window = None
    if earliest is not None:
        trigger_ids = [probe.monitor["id"] for probe in probes
                       if probe.triggered and probe.trip_clock == earliest]
        before, after = spec["capture"]["before"], spec["capture"]["after"]
        lo = max(0, earliest - before)
        hi = min(spec["total"] - 1, earliest + after)
        window = []
        bases = [item["base"] for item in segments]
        for clock in range(lo, hi + 1):
            segment = segments[bisect_right(bases, clock) - 1]
            offset = clock - segment["base"]
            orbit = segment["orbit"]
            pre_state = state_at(orbit, offset)
            observed = orbit["observed"][orbit["states_at"][pre_state]]
            post_state = orbit["next_states"][orbit["states_at"][pre_state]]
            window.append({
                "clock": clock,
                "hold_id": segment["id"],
                "hold_offset": offset,
                "inputs": {name: value for name, value in
                           zip(spec["inputs"], segment["assignment"])},
                "state_before_edge": {name: value for name, value in
                                      zip(engine.flop_names, pre_state)},
                "outputs": {name: value for name, value in
                            zip(spec["outputs"], observed)},
                "state_after_edge": {name: value for name, value in
                                     zip(engine.flop_names, post_state)},
            })

    return {
        "schema_version": SCHEMA_VERSION,
        "total_clocks": spec["total"],
        "monitors": monitor_reports,
        "first_trip_clock": earliest,
        "trigger_monitor_ids": trigger_ids,
        "waveform_window": window,
        "final_state": {name: value for name, value in
                        zip(engine.flop_names, final_state)},
        "summary": {
            "total_clocks": spec["total"],
            "monitors": len(spec["monitors"]),
            "triggered_monitors": sum(1 for probe in probes if probe.triggered),
            "total_mismatches": sum(probe.mismatches for probe in probes),
            "first_trip_clock": earliest,
            "window_rows": 0 if window is None else len(window),
        },
    }
