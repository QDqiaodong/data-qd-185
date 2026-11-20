from .gates import evaluate
from .netlist import CONSTANTS


def replay(gates, flops, outputs, vectors, order):
    state = {name: flop["initial"] for name, flop in sorted(flops.items())}
    activity = {name: {"values_seen": set(), "known_transitions": 0} for name in gates}
    previous = {}
    trace, mismatches = [], []
    comparisons = 0
    for cycle, (vector_id, vector) in enumerate(vectors.items()):
        values = {**CONSTANTS, **vector["inputs"], **state}
        for name in order:
            gate = gates[name]
            values[name] = evaluate(gate["op"], [values[arg] for arg in gate["args"]])
            item = activity[name]
            item["values_seen"].add(values[name])
            if previous.get(name) in ("0", "1") and values[name] in ("0", "1") and previous[name] != values[name]:
                item["known_transitions"] += 1
            previous[name] = values[name]
        observed = {name: values[name] for name in outputs}
        cycle_mismatches = []
        for name, expected in sorted(vector["expect"].items()):
            if expected == "*":
                continue
            comparisons += 1
            if expected != observed[name]:
                failure = {"cycle": cycle, "vector_id": vector_id, "output": name,
                           "expected": expected, "actual": observed[name]}
                mismatches.append(failure)
                cycle_mismatches.append(name)
        next_state = {name: values[flop["d"]] for name, flop in sorted(flops.items())}
        trace.append({"cycle": cycle, "vector_id": vector_id, "inputs": vector["inputs"],
                      "state_before_edge": state, "outputs_before_edge": observed,
                      "state_after_edge": next_state, "mismatched_outputs": cycle_mismatches})
        state = next_state
    return {"trace": trace, "mismatches": mismatches, "final_state": state,
            "gate_activity": {name: {**item, "values_seen": sorted(item["values_seen"])}
                              for name, item in sorted(activity.items())},
            "comparisons": comparisons}
