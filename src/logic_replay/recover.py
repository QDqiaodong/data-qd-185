"""Capture recovery and replay-package handoff (schema v4).

A capture starts from the netlist initial values, has no intermediate resets,
and may omit some applied inputs.  Every missing input cell is either
uniquely forced by trustworthy pre-edge samples (observed somewhere in the
capture) or remains ambiguous.  For each requested review window this module
also checks whether a second test bench, starting from the netlist initial
values, can reach the recovered entry state with a contiguous warmup taken
from the capture, and if so emits a self-contained v1 replay package.
"""
import itertools

from .gates import evaluate
from .io import fields, label, records
from .netlist import BITS, CONSTANTS, compile_circuit

MAX_FLOPS = 6
MAX_CYCLES = 256
MAX_MISSING = 3
MAX_WINDOWS = 32
VALUE_ORDER = {"0": 0, "1": 1, "X": 2}


def _ordered(values):
    return sorted(values, key=lambda value: VALUE_ORDER[value])


def compile_recover(data):
    """Validate the whole capture before any recovery analysis happens."""
    fields(data, ("inputs", "gates", "flops", "outputs", "rows", "windows"))
    inputs, gates, flops, outputs, order = compile_circuit(data)
    if len(flops) > MAX_FLOPS:
        raise ValueError("recover capture allows at most 6 flops")

    rows = records(data["rows"], "row", 1, MAX_CYCLES)
    input_set, output_set = set(inputs), set(outputs)
    missing = []
    for row in rows.values():
        fields(row, ("id", "inputs", "observed"))
        given = row["inputs"]
        if not isinstance(given, dict):
            raise ValueError("row.inputs must be an object")
        if set(given) - input_set:
            raise ValueError("row.inputs references undeclared inputs")
        for value in given.values():
            if not isinstance(value, str) or value not in BITS:
                raise ValueError('row.input value must be string "0", "1" or "X"')
        if len(inputs) - len(given) > MAX_MISSING:
            raise ValueError("a row may omit at most 3 inputs")
        observed = row["observed"]
        if not isinstance(observed, dict):
            raise ValueError("row.observed must be an object")
        if set(observed) - output_set:
            raise ValueError("row.observed references undeclared outputs")
        for value in observed.values():
            if not isinstance(value, str) or value not in BITS:
                raise ValueError('row.observed value must be string "0", "1" or "X"')
        missing.append([name for name in inputs if name not in given])

    requests_raw = records(data["windows"], "window", 0, MAX_WINDOWS)
    cycle_order = list(rows)
    position = {cycle_id: index for index, cycle_id in enumerate(cycle_order)}
    requests = []
    for window_id, row in requests_raw.items():
        fields(row, ("id", "from", "through"))
        start = label(row["from"], "window.from")
        stop = label(row["through"], "window.through")
        if start not in position:
            raise ValueError(f"window references unknown cycle ID: {start}")
        if stop not in position:
            raise ValueError(f"window references unknown cycle ID: {stop}")
        if position[start] > position[stop]:
            raise ValueError("window range must follow capture order")
        requests.append({"id": window_id, "from": start, "through": stop,
                         "from_index": position[start],
                         "through_index": position[stop]})

    ordered = [rows[cycle_id] for cycle_id in cycle_order]
    return {"inputs": inputs, "gates": gates, "flops": flops, "outputs": outputs,
            "order": order, "rows": ordered, "missing": missing,
            "windows": requests}


def _assignments(inputs, row, missing):
    """Every full input assignment consistent with the recorded cells."""
    fixed = row["inputs"]
    result = []
    for combo in itertools.product(("0", "1"), repeat=len(missing)):
        mapping = dict(fixed)
        mapping.update(zip(missing, combo))
        result.append((tuple(mapping[name] for name in inputs), mapping))
    return result


class _Engine:
    """Deterministic one-cycle transition and pre-edge observation."""

    def __init__(self, spec):
        self.inputs = spec["inputs"]
        self.gates = spec["gates"]
        self.flops = spec["flops"]
        self.outputs = spec["outputs"]
        self.order = spec["order"]
        self.flop_names = sorted(self.flops)
        self.initial = tuple(self.flops[name]["initial"] for name in self.flop_names)
        self._cache = {}

    def step(self, assignment, state):
        key = (assignment, state)
        cached = self._cache.get(key)
        if cached is None:
            values = dict(CONSTANTS)
            values.update(zip(self.inputs, assignment))
            values.update(zip(self.flop_names, state))
            for name in self.order:
                gate = self.gates[name]
                values[name] = evaluate(gate["op"], [values[arg] for arg in gate["args"]])
            observed = {name: values[name] for name in self.outputs}
            next_state = tuple(values[self.flops[name]["d"]] for name in self.flop_names)
            cached = (observed, next_state)
            self._cache[key] = cached
        return cached

    def state_dict(self, state):
        return {name: value for name, value in zip(self.flop_names, state)}

    def state_key(self, state):
        return tuple(VALUE_ORDER[value] for value in state)


def _matches(observed, samples):
    return all(observed[name] == want for name, want in samples.items())


def _analyze(spec, engine):
    """Forward reachability, backward feasibility and per-cell candidates."""
    rows, inputs = spec["rows"], spec["inputs"]
    assignment_sets = [_assignments(inputs, row, spec["missing"][index])
                       for index, row in enumerate(rows)]
    count = len(rows)
    forward = [set() for _ in range(count + 1)]
    forward[0].add(engine.initial)
    contradiction = None
    for index, row in enumerate(rows):
        following = set()
        for state in forward[index]:
            for assignment, _mapping in assignment_sets[index]:
                observed, next_state = engine.step(assignment, state)
                if _matches(observed, row["observed"]):
                    following.add(next_state)
        forward[index + 1] = following
        if not following:
            contradiction = index
            break
    if contradiction is not None:
        return {"contradiction": contradiction, "forward": forward,
                "assignments": assignment_sets, "core": None, "possible": None}

    core = [set() for _ in range(count + 1)]
    core[count] = set(forward[count])
    for index in range(count - 1, -1, -1):
        row = rows[index]
        keep = set()
        for state in forward[index]:
            for assignment, _mapping in assignment_sets[index]:
                observed, next_state = engine.step(assignment, state)
                if _matches(observed, row["observed"]) and next_state in core[index + 1]:
                    keep.add(state)
                    break
        core[index] = keep

    possible = {}
    for index, row in enumerate(rows):
        cells = spec["missing"][index]
        if not cells:
            continue
        for state in core[index]:
            for assignment, mapping in assignment_sets[index]:
                observed, next_state = engine.step(assignment, state)
                if _matches(observed, row["observed"]) and next_state in core[index + 1]:
                    for name in cells:
                        possible.setdefault((index, name), set()).add(mapping[name])
    return {"contradiction": None, "forward": forward, "assignments": assignment_sets,
            "core": core, "possible": possible}


def _cells(spec, analysis, index, row):
    cells = []
    for name in spec["inputs"]:
        if name in row["inputs"]:
            cells.append({"input": name, "status": "recorded",
                          "value": row["inputs"][name]})
            continue
        values = analysis["possible"].get((index, name), set())
        if len(values) == 1:
            cells.append({"input": name, "status": "recovered",
                          "value": next(iter(values))})
        else:
            cells.append({"input": name, "status": "ambiguous",
                          "candidates": _ordered(values)})
    return cells


def _known_inputs(spec, analysis, index):
    """Full input map for a cycle, or None if some cell is still ambiguous."""
    row = spec["rows"][index]
    result = {}
    for name in spec["inputs"]:
        if name in row["inputs"]:
            result[name] = row["inputs"][name]
            continue
        values = analysis["possible"].get((index, name), set())
        if len(values) != 1:
            return None
        result[name] = next(iter(values))
    return result


def _reaches(spec, engine, analysis, start, stop, target):
    """Simulate the contiguous warmup start..stop-1 from initial values."""
    state = engine.initial
    for index in range(start, stop):
        mapping = _known_inputs(spec, analysis, index)
        if mapping is None:
            return False
        assignment = tuple(mapping[name] for name in spec["inputs"])
        _observed, state = engine.step(assignment, state)
    return state == target


def _package_netlist(spec):
    return {
        "inputs": list(spec["inputs"]),
        "gates": [{"id": gate_id, "op": gate["op"], "args": list(gate["args"])}
                  for gate_id, gate in spec["gates"].items()],
        "flops": [{"id": flop_id, "d": flop["d"], "initial": flop["initial"]}
                  for flop_id, flop in spec["flops"].items()],
        "outputs": list(spec["outputs"]),
    }


def _package(spec, analysis, start, stop, first, last):
    vectors = []
    for index in range(start, stop):
        mapping = _known_inputs(spec, analysis, index)
        vectors.append({"id": spec["rows"][index]["id"], "inputs": mapping, "expect": {}})
    for index in range(first, last + 1):
        row = spec["rows"][index]
        mapping = _known_inputs(spec, analysis, index)
        expect = {name: row["observed"][name] for name in spec["outputs"]
                  if name in row["observed"]}
        vectors.append({"id": row["id"], "inputs": mapping, "expect": expect})
    return {**_package_netlist(spec), "vectors": vectors}


def _blocked(request, reason, **detail):
    return {"id": request["id"], "from": request["from"],
            "through": request["through"], "status": "blocked",
            "blocking_reason": reason, **detail}


def _handoff(spec, engine, analysis, request):
    first, last = request["from_index"], request["through_index"]
    rows = spec["rows"]

    ambiguous = []
    for index in range(first, last + 1):
        for name in spec["missing"][index]:
            values = analysis["possible"].get((index, name), set())
            if len(values) != 1:
                ambiguous.append({"cycle": rows[index]["id"], "input": name,
                                  "candidates": _ordered(values)})
    if ambiguous:
        return _blocked(request, "window_input_ambiguity",
                        ambiguous_cells=ambiguous)

    entry = analysis["core"][first]
    if len(entry) != 1:
        states = [engine.state_dict(state)
                  for state in sorted(entry, key=engine.state_key)]
        return _blocked(request, "entry_state_ambiguity", entry_states=states)

    target = next(iter(entry))
    start = None
    for candidate in range(first, -1, -1):
        if _reaches(spec, engine, analysis, candidate, first, target):
            start = candidate
            break
    if start is None:
        return _blocked(request, "no_eligible_warmup",
                        entry_state=engine.state_dict(target))

    warmup_ids = [rows[index]["id"] for index in range(start, first)]
    window_ids = [rows[index]["id"] for index in range(first, last + 1)]
    return {"id": request["id"], "from": request["from"],
            "through": request["through"], "status": "handoff",
            "entry_state": engine.state_dict(target),
            "warmup_cycle_ids": warmup_ids,
            "warmup_length": first - start,
            "window_cycle_ids": window_ids,
            "replay_package": _package(spec, analysis, start, first, first, last)}


def _summary(spec, status, analysis, windows):
    cells = {"recorded": 0, "recovered": 0, "ambiguous": 0}
    if analysis["core"] is not None:
        for cycle in analysis["cycles"]:
            for cell in cycle["inputs"]:
                cells[cell["status"]] += 1
    handoffs = sum(1 for window in windows if window["status"] == "handoff")
    return {"status": status,
            "cycles": len(spec["rows"]),
            "inputs": len(spec["inputs"]),
            "flops": len(spec["flops"]),
            "windows": len(windows),
            "handoffs": handoffs,
            "blocked": len(windows) - handoffs,
            "first_contradiction": (None if status == "consistent"
                                    else spec["rows"][analysis["contradiction"]]["id"]),
            "recorded_input_cells": cells["recorded"],
            "recovered_input_cells": cells["recovered"],
            "ambiguous_input_cells": cells["ambiguous"]}


def run_recover(data):
    spec = compile_recover(data)
    engine = _Engine(spec)
    analysis = _analyze(spec, engine)

    if analysis["contradiction"] is not None:
        windows = [_blocked(request, "capture_contradiction")
                   for request in spec["windows"]]
        return {"schema_version": 4, "status": "contradiction",
                "first_contradiction": spec["rows"][analysis["contradiction"]]["id"],
                "first_contradiction_index": analysis["contradiction"],
                "cycles": [], "windows": windows,
                "summary": _summary(spec, "contradiction", analysis, windows)}

    analysis["cycles"] = [{"cycle": index, "id": row["id"],
                           "inputs": _cells(spec, analysis, index, row)}
                          for index, row in enumerate(spec["rows"])]
    windows = [_handoff(spec, engine, analysis, request)
               for request in spec["windows"]]
    return {"schema_version": 4, "status": "consistent",
            "first_contradiction": None,
            "cycles": analysis["cycles"], "windows": windows,
            "summary": _summary(spec, "consistent", analysis, windows)}
