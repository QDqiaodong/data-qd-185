"""Compile one-bit combinational gates with synchronous D flip-flops."""
import heapq
import re
from .io import fields, records, rows

BITS = {"0", "1", "X"}
ARITY = {"BUF": 1, "NOT": 1, "AND": 2, "OR": 2, "XOR": 2, "NAND": 2, "NOR": 2}
CONSTANTS = {"0": "0", "1": "1", "X": "X"}


def net(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", value):
        raise ValueError("net name must be ASCII identifier <=64 characters")
    return value


def bit(value, wildcard=False):
    if not isinstance(value, str) or value not in (BITS | {"*"} if wildcard else BITS):
        raise ValueError("logic value must be string 0, 1, X" + (" or *" if wildcard else ""))
    return value


def compile_circuit(data):
    """Validate the netlist part (inputs/gates/flops/outputs) shared by v1 and v2."""
    inputs = rows(data["inputs"], "inputs", 1, 64)
    for name in inputs:
        net(name)
    if len(set(inputs)) != len(inputs):
        raise ValueError("duplicate input")
    gates = records(data["gates"], "gate", 0, 200)
    flops = records(data["flops"], "flop", 0, 64)
    all_ids = inputs + list(gates) + list(flops)
    for name in all_ids:
        net(name)
    if len(set(all_ids)) != len(all_ids):
        raise ValueError("multiple drivers for net")
    signals = set(all_ids) | set(CONSTANTS)
    for gate in gates.values():
        fields(gate, ("id", "op", "args"))
        op = gate["op"]
        if not isinstance(op, str) or op not in ARITY:
            raise ValueError("unknown gate operation")
        args = rows(gate["args"], "gate args", ARITY[op], ARITY[op])
        if any(not isinstance(value, str) or value not in signals for value in args):
            raise ValueError("unknown gate input")
    for flop in flops.values():
        fields(flop, ("id", "d", "initial"))
        if not isinstance(flop["d"], str) or flop["d"] not in signals:
            raise ValueError("unknown flop D input")
        bit(flop["initial"])
    outputs = rows(data["outputs"], "outputs", 1, 64)
    if any(not isinstance(value, str) or value not in signals for value in outputs):
        raise ValueError("unknown output")
    if len(set(outputs)) != len(outputs):
        raise ValueError("duplicate output")
    dependencies = {name: set(gate["args"]) & set(gates) for name, gate in gates.items()}
    consumers = {name: set() for name in gates}
    for name, parents in dependencies.items():
        for parent in parents:
            consumers[parent].add(name)
    ready = [name for name, parents in dependencies.items() if not parents]
    heapq.heapify(ready)
    order = []
    while ready:
        name = heapq.heappop(ready)
        order.append(name)
        for child in sorted(consumers[name]):
            dependencies[child].remove(name)
            if not dependencies[child]:
                heapq.heappush(ready, child)
    if len(order) != len(gates):
        raise ValueError("combinational cycle")
    return inputs, gates, flops, outputs, order


def check_vectors(data, inputs, outputs, minimum=1, maximum=1000):
    vectors = records(data, "vector", minimum, maximum)
    for vector in vectors.values():
        fields(vector, ("id", "inputs", "expect"))
        fields(vector["inputs"], inputs)
        if not isinstance(vector["expect"], dict) or set(vector["expect"]) - set(outputs):
            raise ValueError("expect must reference only declared outputs")
        for value in vector["inputs"].values():
            bit(value)
        for value in vector["expect"].values():
            bit(value, True)
    return vectors


def compile_netlist(data):
    fields(data, ("inputs", "gates", "flops", "outputs", "vectors"))
    inputs, gates, flops, outputs, order = compile_circuit(data)
    vectors = check_vectors(data["vectors"], inputs, outputs, 1, 1000)
    return inputs, gates, flops, outputs, vectors, order
