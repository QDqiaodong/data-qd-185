"""Fault-coverage campaign scheduling (schema v2).

Selects an ordered subset of vector fragments with optional resets between
them so that, under a cycle budget, the number of distinct detected
single stuck-at gate faults is maximized, then total cost minimized, then
the (fragment_id, reset_before) sequence lexicographically minimized.
"""
from .gates import evaluate
from .io import fields, integer, records, rows
from .netlist import CONSTANTS, check_vectors, compile_circuit

KNOWN = ("0", "1")


def compile_campaign(data):
    """Validate the whole campaign input before any scheduling happens."""
    fields(data, ("inputs", "gates", "flops", "outputs", "fragments",
                  "faults", "cycle_budget", "reset_cost"))
    inputs, gates, flops, outputs, order = compile_circuit(data)
    fragments = {}
    for fid, fragment in records(data["fragments"], "fragment", 1, 7).items():
        fields(fragment, ("id", "vectors"))
        fragments[fid] = {"id": fid,
                          "vectors": check_vectors(fragment["vectors"], inputs, outputs, 1, 8)}
    faults = []
    seen = set()
    for row in rows(data["faults"], "faults", 1, 8):
        fields(row, ("gate", "stuck_at"))
        gate = row["gate"]
        if not isinstance(gate, str) or gate not in gates:
            raise ValueError("fault gate must be a declared gate")
        stuck = row["stuck_at"]
        if not isinstance(stuck, str) or stuck not in KNOWN:
            raise ValueError('fault stuck_at must be string "0" or "1"')
        if (gate, stuck) in seen:
            raise ValueError(f"duplicate fault: {gate} stuck_at {stuck}")
        seen.add((gate, stuck))
        faults.append((gate, stuck))
    budget = integer(data["cycle_budget"], "cycle_budget", 0, 1_000_000)
    reset_cost = integer(data["reset_cost"], "reset_cost", 1, 1_000_000)
    return {"inputs": inputs, "gates": gates, "flops": flops, "outputs": outputs,
            "order": order, "fragments": fragments, "faults": faults,
            "cycle_budget": budget, "reset_cost": reset_cost}


def simulate_fragment(gates, flops, outputs, order, vectors, state, fault):
    """Replay one fragment from flop-state tuple `state` (sorted flop order).

    `fault` is None (reference) or a (gate, stuck_at) pair forced every cycle.
    Returns (end_state_tuple, per-cycle observed-output dicts).
    """
    names = sorted(flops)
    current = dict(zip(names, state))
    observations = []
    for vector in vectors.values():
        values = {**CONSTANTS, **vector["inputs"], **current}
        for name in order:
            if fault is not None and name == fault[0]:
                values[name] = fault[1]
            else:
                gate = gates[name]
                values[name] = evaluate(gate["op"], [values[arg] for arg in gate["args"]])
        observations.append({name: values[name] for name in outputs})
        current = {name: values[flops[name]["d"]] for name in names}
    return tuple(current[name] for name in names), observations


def plan_schedule(spec):
    """Exhaustively search every ordered fragment subset with reset flags.

    Returns the optimal steps as a list of (fragment_id, reset_before).
    Branch-and-bound over the global optimum: prunes only branches that
    cannot improve it (over budget, reference-invalid, or provably unable
    to beat the best coverage/cost found so far).
    """
    gates, flops, outputs, order = (spec[key] for key in ("gates", "flops", "outputs", "order"))
    fragments, faults = spec["fragments"], spec["faults"]
    budget, reset_cost = spec["cycle_budget"], spec["reset_cost"]
    initial = tuple(flops[name]["initial"] for name in sorted(flops))
    frag_ids = sorted(fragments)
    frag_len = {fid: len(fragments[fid]["vectors"]) for fid in frag_ids}
    # Per fragment and cycle: expectation pairs for reference checking and
    # the stricter "0"/"1" subset usable as fault-detection points.
    expect_all, expect_known = {}, {}
    for fid in frag_ids:
        rows_all, rows_known = [], []
        for vector in fragments[fid]["vectors"].values():
            rows_all.append([(name, want) for name, want in vector["expect"].items()
                             if want != "*"])
            rows_known.append([(name, want) for name, want in vector["expect"].items()
                               if want in KNOWN])
        expect_all[fid], expect_known[fid] = rows_all, rows_known
    if not any(pairs for rows in expect_known.values() for pairs in rows):
        return []  # no "0"/"1" expectation anywhere: detection is impossible
    # Static over-approximation: a fault can only be detected in a fragment
    # if its gate's downstream cone (through gates and flop D->Q) reaches an
    # output the fragment compares against a "0"/"1" expectation.
    readers = {net: set() for net in list(gates) + list(flops) + spec["inputs"]}
    for gid, gate in gates.items():
        for arg in gate["args"]:
            if arg in readers:
                readers[arg].add(gid)
    for flop_id, flop in flops.items():
        if flop["d"] in readers:
            readers[flop["d"]].add(flop_id)

    def cone(gate):
        seen, stack = set(), [gate]
        while stack:
            net = stack.pop()
            if net not in seen:
                seen.add(net)
                stack.extend(readers.get(net, ()))
        return seen

    fault_cones = [cone(gate) for gate, _ in faults]
    static_possible = {}
    for fid in frag_ids:
        watched = {name for rows in expect_known[fid] for name, _ in rows}
        static_possible[fid] = {index for index, affected in enumerate(fault_cones)
                                if affected & watched}
    mask_possible = [frozenset()] * (1 << len(frag_ids))
    for bit, fid in enumerate(frag_ids):
        for mask in range(1 << len(frag_ids)):
            if mask & (1 << bit):
                mask_possible[mask] |= static_possible[fid]
    cache = {}
    expand_cache = {}

    def transition(fault, fid, state):
        key = (fault, fid, state)
        if key not in cache:
            cache[key] = simulate_fragment(gates, flops, outputs, order,
                                           fragments[fid]["vectors"], state, fault)
        return cache[key]

    def expand(fid, reset, ref_state, fault_states):
        """Simulate one scheduled step; None if reference-invalid."""
        key = (fid, reset, ref_state, fault_states)
        if key in expand_cache:
            return expand_cache[key]
        start = initial if reset else ref_state
        end_ref, ref_obs = transition(None, fid, start)
        valid = all(obs[name] == want
                    for cycle, obs in enumerate(ref_obs)
                    for name, want in expect_all[fid][cycle])
        if not valid:
            expand_cache[key] = None
            return None
        new_states, detected = [], set()
        for index, fault in enumerate(faults):
            if fault_states[index] is None:
                new_states.append(None)  # state irrelevant once detected
                continue
            fstart = initial if reset else fault_states[index]
            fend, fault_obs = transition(fault, fid, fstart)
            new_states.append(fend)
            for cycle, pairs in enumerate(expect_known[fid]):
                if any(ref_obs[cycle][name] in KNOWN
                       and fault_obs[cycle][name] in KNOWN
                       and ref_obs[cycle][name] != fault_obs[cycle][name]
                       for name, _ in pairs):
                    detected.add(index)
                    break
        expand_cache[key] = (end_ref, tuple(new_states), frozenset(detected))
        return expand_cache[key]

    best = [None]

    def dfs(remaining, ref_state, fault_states, cost, detected, steps):
        key = (-len(detected), cost, tuple(steps))
        if best[0] is None or key < best[0][0]:
            best[0] = (key, tuple(steps))
        best_count, best_cost = -best[0][0][0], best[0][0][1]
        reachable = len(detected | mask_possible[remaining])
        if reachable < best_count or (reachable == best_count and cost >= best_cost):
            return  # extensions cannot beat the incumbent
        for bit, fid in enumerate(frag_ids):
            if not remaining & (1 << bit):
                continue
            for reset in ((False,) if not steps else (False, True)):
                new_cost = cost + frag_len[fid] + (reset_cost if reset else 0)
                if new_cost > budget:
                    continue
                expansion = expand(fid, reset, ref_state, fault_states)
                if expansion is None:
                    continue  # extensions stay reference-invalid
                end_ref, new_states, own = expansion
                merged = detected | own
                child_states = tuple(None if index in merged else state
                                     for index, state in enumerate(new_states))
                dfs(remaining & ~(1 << bit), end_ref, child_states, new_cost,
                    merged, steps + [(fid, reset)])

    dfs((1 << len(frag_ids)) - 1, initial, tuple(initial for _ in faults), 0, set(), [])
    return list(best[0][1])


def collect_witnesses(spec, steps):
    """Re-simulate the chosen schedule and record each fault's first
    detection point (earliest cycle, then lexicographically smallest
    output name). Witnesses always come from the final schedule."""
    gates, flops, outputs, order = (spec[key] for key in ("gates", "flops", "outputs", "order"))
    fragments, faults = spec["fragments"], spec["faults"]
    initial = tuple(flops[name]["initial"] for name in sorted(flops))
    ref_state = initial
    fault_states = [initial] * len(faults)
    witnesses = {}
    for fid, reset in steps:
        if reset:
            ref_state = initial
            fault_states = [initial] * len(faults)
        vectors = fragments[fid]["vectors"]
        ref_state, ref_obs = simulate_fragment(gates, flops, outputs, order,
                                               vectors, ref_state, None)
        fault_obs = []
        for index, fault in enumerate(faults):
            fault_states[index], obs = simulate_fragment(gates, flops, outputs, order,
                                                         vectors, fault_states[index], fault)
            fault_obs.append(obs)
        for cycle, (vector_id, vector) in enumerate(vectors.items()):
            points = sorted(name for name, want in vector["expect"].items() if want in KNOWN)
            for index in range(len(faults)):
                if index in witnesses:
                    continue
                for name in points:
                    ref_value = ref_obs[cycle][name]
                    faulty_value = fault_obs[index][cycle][name]
                    if (ref_value in KNOWN and faulty_value in KNOWN
                            and ref_value != faulty_value):
                        witnesses[index] = {"fragment_id": fid, "vector_id": vector_id,
                                            "output": name, "reference_value": ref_value,
                                            "faulty_value": faulty_value}
                        break
    return witnesses


def run_campaign(data):
    spec = compile_campaign(data)
    steps = plan_schedule(spec)
    witnesses = collect_witnesses(spec, steps)
    detected, undetected = [], []
    for index, (gate, stuck) in enumerate(spec["faults"]):
        row = {"gate": gate, "stuck_at": stuck}
        if index in witnesses:
            detected.append({**row, "witness": witnesses[index]})
        else:
            undetected.append(row)
    detected.sort(key=lambda row: (row["gate"], row["stuck_at"]))
    undetected.sort(key=lambda row: (row["gate"], row["stuck_at"]))
    cycles = sum(len(spec["fragments"][fid]["vectors"]) for fid, _ in steps)
    resets = sum(1 for _, reset in steps if reset)
    total_cost = cycles + resets * spec["reset_cost"]
    return {"schema_version": 2,
            "schedule": [{"fragment_id": fid, "reset_before": reset} for fid, reset in steps],
            "total_cost": total_cost,
            "detected_faults": detected,
            "undetected_faults": undetected,
            "summary": {"fragments_total": len(spec["fragments"]),
                        "fragments_selected": len(steps),
                        "cycles": cycles,
                        "resets": resets,
                        "reset_cost": spec["reset_cost"],
                        "cycle_budget": spec["cycle_budget"],
                        "total_cost": total_cost,
                        "faults_total": len(spec["faults"]),
                        "faults_detected": len(detected),
                        "all_faults_detected": not undetected}}
