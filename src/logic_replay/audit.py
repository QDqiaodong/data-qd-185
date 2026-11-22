"""Drained-capture transaction audit (schema v6).

The bench no longer carries full sequence numbers: only the two handshake
channels, short reusing tags and the request class.  A capture claims no
outstanding request before its first row and no pending response after its
last row, so every request and every response must be used exactly once in a
complete pairing.  This module extracts handshake events from the *actual*
pre-edge outputs of a normal v1 replay, refuses to certify anything when the
observation is insufficient, and otherwise reasons over the set of all
complete pairings: which request/response edges can belong to at least one
complete pairing, which pairings are certain, and the minimum/maximum number
of late transfers achievable.

Late means latency strictly greater than the class deadline; an edge past the
deadline is still a legal identity as long as it stays within ``max_age``.
"""
import sys
from collections import deque

from .io import fields, integer
from .netlist import compile_netlist
from .simulate import replay

SCHEMA_VERSION = 6
MAX_TAG_BITS = 8
# At most 1000 events per side, so an alternating-path DFS cannot exceed this.
_RECURSION_LIMIT = 4000
_HANDSHAKE_UNKNOWN = "handshake_unknown"
_TAG_UNKNOWN = "tag_unknown"
_CLASS_UNKNOWN = "class_unknown"


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #

def _signal_ref(value, name, outputs):
    if not isinstance(value, str) or value not in outputs:
        raise ValueError(f"{name} must reference a declared source output net")
    return value


def _channel_config(section, name, outputs, with_class):
    if not isinstance(section, dict):
        raise ValueError(f"{name} config must be an object")
    required = ("valid", "ready", "tag_bits") + (("class_bit",) if with_class else ())
    fields(section, required)
    valid = _signal_ref(section["valid"], f"{name}.valid", outputs)
    ready = _signal_ref(section["ready"], f"{name}.ready", outputs)
    tag_bits = section["tag_bits"]
    if not isinstance(tag_bits, list) or not 1 <= len(tag_bits) <= MAX_TAG_BITS:
        raise ValueError(f"{name}.tag_bits requires 1..{MAX_TAG_BITS} signal references")
    resolved_tags = [_signal_ref(value, f"{name}.tag_bits", outputs) for value in tag_bits]
    if len(set(resolved_tags)) != len(resolved_tags):
        raise ValueError(f"{name}.tag_bits must not reference the same net twice")
    config = {"valid": valid, "ready": ready, "tag_bits": resolved_tags}
    if with_class:
        config["class_bit"] = _signal_ref(section["class_bit"], f"{name}.class_bit", outputs)
    return config


def compile_audit(data):
    """Validate the whole audit document before any replay or analysis."""
    fields(data, ("source", "request", "response", "max_age", "deadlines", "late_budget"))
    source = data["source"]
    if not isinstance(source, dict):
        raise ValueError("source must be a complete v1 replay input object")
    _inputs, gates, flops, outputs, vectors, order = compile_netlist(source)
    request_cfg = _channel_config(data["request"], "request", outputs, True)
    response_cfg = _channel_config(data["response"], "response", outputs, False)
    if len(request_cfg["tag_bits"]) != len(response_cfg["tag_bits"]):
        raise ValueError("request and response tag widths must match")
    max_age = integer(data["max_age"], "max_age", 1, 999)
    deadlines = data["deadlines"]
    if not isinstance(deadlines, dict):
        raise ValueError("deadlines must be an object")
    fields(deadlines, ("0", "1"))
    deadline_by_class = {}
    for key in ("0", "1"):
        deadline_by_class[key] = integer(deadlines[key], f"deadlines.{key}", 1, max_age)
    late_budget = integer(data["late_budget"], "late_budget", 0, 1000)
    return {"gates": gates, "flops": flops, "outputs": outputs, "vectors": vectors,
            "order": order, "request": request_cfg, "response": response_cfg,
            "tag_width": len(request_cfg["tag_bits"]), "max_age": max_age,
            "deadlines": deadline_by_class, "late_budget": late_budget}


# --------------------------------------------------------------------------- #
# Event extraction from actual pre-edge outputs
# --------------------------------------------------------------------------- #

def _read_channel(channel, config, trace, with_class, issues, events):
    """Apply the handshake/observation rules for one side, cycle by cycle."""
    for cycle, row in enumerate(trace):
        observed = row["outputs_before_edge"]
        vector_id = row["vector_id"]
        valid = observed[config["valid"]]
        ready = observed[config["ready"]]
        # A literal "0" on either side means no handshake; every other bit of
        # this channel is deliberately not inspected on such cycles.
        if valid == "0" or ready == "0":
            continue
        if valid == "X" or ready == "X":
            issues.append({"cycle": cycle, "vector_id": vector_id, "channel": channel,
                           "reason": _HANDSHAKE_UNKNOWN})
            continue
        # Both are "1": the handshake is confirmed.  Unknown payload bits now
        # matter and make the whole batch unobservable.
        tag_cells = [observed[name] for name in config["tag_bits"]]
        if any(bit == "X" for bit in tag_cells):
            issues.append({"cycle": cycle, "vector_id": vector_id, "channel": channel,
                           "reason": _TAG_UNKNOWN})
            continue
        event = {"cycle": cycle, "vector_id": vector_id, "tag": "".join(tag_cells)}
        if with_class:
            class_value = observed[config["class_bit"]]
            if class_value == "X":
                issues.append({"cycle": cycle, "vector_id": vector_id,
                               "channel": channel, "reason": _CLASS_UNKNOWN})
                continue
            event["class"] = class_value
        events.append(event)


def extract_events(spec, trace):
    issues, request_events, response_events = [], [], []
    _read_channel("request", spec["request"], trace, True, issues, request_events)
    _read_channel("response", spec["response"], trace, False, issues, response_events)
    issues.sort(key=lambda item: (item["cycle"], item["channel"]))
    return issues, request_events, response_events


# --------------------------------------------------------------------------- #
# Bipartite matching machinery
# --------------------------------------------------------------------------- #

def hopcroft_karp(adj, left_size, right_size):
    """Maximum bipartite matching via Hopcroft-Karp in O(E sqrt(V)).

    The DFS depth is bounded by the alternating-path length (<= the event
    count); the recursion limit is raised locally and restored afterwards.
    """
    match_left = [-1] * left_size
    match_right = [-1] * right_size

    def bfs():
        dist = [-1] * left_size
        queue = deque()
        for u in range(left_size):
            if match_left[u] < 0:
                dist[u] = 0
                queue.append(u)
        reachable_free = False
        while queue:
            u = queue.popleft()
            for v in adj[u]:
                partner = match_right[v]
                if partner < 0:
                    reachable_free = True
                elif dist[partner] < 0:
                    dist[partner] = dist[u] + 1
                    queue.append(partner)
        return dist, reachable_free

    def dfs(u, dist):
        for v in adj[u]:
            partner = match_right[v]
            if partner < 0 or (dist[partner] == dist[u] + 1 and dfs(partner, dist)):
                match_left[u] = v
                match_right[v] = u
                return True
        dist[u] = -1
        return False

    previous_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(previous_limit, _RECURSION_LIMIT))
    try:
        while True:
            dist, reachable_free = bfs()
            if not reachable_free:
                break
            for u in range(left_size):
                if match_left[u] < 0:
                    dfs(u, dist)
    finally:
        sys.setrecursionlimit(previous_limit)
    size = sum(1 for v in match_left if v >= 0)
    return match_left, match_right, size


def classify_edges(adj, left_size, right_size):
    """Return the edges that belong to at least one maximum matching.

    Take a maximum matching M and orient the graph: unmatched edges L->R,
    matched edges R->L.  An edge (u,rv) not in M is in some maximum matching
    exactly when u and rv share a strongly connected component (alternating
    cycle flip), when u is reachable from an M-free left vertex (alternating
    path starting at a free left vertex), or when rv can reach an M-free
    right vertex (alternating path ending at a free right vertex).
    """
    match_left, match_right, size = hopcroft_karp(adj, left_size, right_size)
    allowed = {(u, match_left[u]) for u in range(left_size) if match_left[u] >= 0}

    vertices = left_size + right_size

    def right_index(v):
        return left_size + v

    graph = [[] for _ in range(vertices)]
    reverse_graph = [[] for _ in range(vertices)]
    for u, edges in enumerate(adj):
        for v in edges:
            rv = right_index(v)
            if match_left[u] == v:
                graph[rv].append(u)
                reverse_graph[u].append(rv)
            else:
                graph[u].append(rv)
                reverse_graph[rv].append(u)

    # Iterative Tarjan SCC.
    index_counter = [0]
    stack, lowlink, index, on_stack = [], [0] * vertices, [-1] * vertices, [False] * vertices
    components = [-1] * vertices

    def strongconnect(root):
        work = [(root, 0)]
        index[root] = lowlink[root] = index_counter[0]
        index_counter[0] += 1
        stack.append(root)
        on_stack[root] = True
        while work:
            node, cursor = work[-1]
            if cursor < len(graph[node]):
                successor = graph[node][cursor]
                work[-1] = (node, cursor + 1)
                if index[successor] < 0:
                    index[successor] = lowlink[successor] = index_counter[0]
                    index_counter[0] += 1
                    stack.append(successor)
                    on_stack[successor] = True
                    work.append((successor, 0))
                elif on_stack[successor]:
                    lowlink[node] = min(lowlink[node], index[successor])
            else:
                if lowlink[node] == index[node]:
                    component_id = node
                    while True:
                        member = stack.pop()
                        on_stack[member] = False
                        components[member] = component_id
                        if member == node:
                            break
                work.pop()
                if work:
                    parent = work[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[node])

    for vertex in range(vertices):
        if index[vertex] < 0:
            strongconnect(vertex)

    def bfs(seeds, neighbors):
        seen = [False] * vertices
        queue = deque(seeds)
        for seed in seeds:
            seen[seed] = True
        while queue:
            node = queue.popleft()
            for successor in neighbors[node]:
                if not seen[successor]:
                    seen[successor] = True
                    queue.append(successor)
        return seen

    free_left = [u for u in range(left_size) if match_left[u] < 0]
    free_right = [right_index(v) for v in range(right_size) if match_right[v] < 0]
    reachable_from_free_left = bfs(free_left, graph)
    can_reach_free_right = bfs(free_right, reverse_graph)

    for u, edges in enumerate(adj):
        for v in edges:
            if match_left[u] == v:
                continue
            rv = right_index(v)
            if (reachable_from_free_left[u] or can_reach_free_right[rv]
                    or components[u] == components[rv]):
                allowed.add((u, v))
    return allowed, match_left, size


# --------------------------------------------------------------------------- #
# Audit analysis
# --------------------------------------------------------------------------- #

def _build_graph(request_events, response_events, max_age):
    """Compatibility adjacency: equal tag and latency in [1, max_age]."""
    adjacency = [[] for _ in request_events]
    for u, request in enumerate(request_events):
        for v, response in enumerate(response_events):
            latency = response["cycle"] - request["cycle"]
            if (request["tag"] == response["tag"] and 1 <= latency <= max_age):
                adjacency[u].append(v)
    return adjacency


def _public_event(event, with_class):
    row = {"cycle": event["cycle"], "vector_id": event["vector_id"], "tag": event["tag"]}
    if with_class:
        row["class"] = event["class"]
    return row


def _witness(match_left, request_events, response_events):
    return [{"request_cycle": request_events[u]["cycle"],
             "response_cycle": response_events[v]["cycle"]}
            for u, v in enumerate(match_left)]


def _analyze_pairing(spec, request_events, response_events):
    n_left, n_right = len(request_events), len(response_events)
    adjacency = _build_graph(request_events, response_events, spec["max_age"])
    allowed, _match_left, maximum = classify_edges(adjacency, n_left, n_right)
    if n_left != n_right or maximum != n_left:
        return {"pairable": False, "maximum_matching": maximum}

    allowed_by_request = [[] for _ in range(n_left)]
    for u, v in allowed:
        allowed_by_request[u].append(v)
    for edges in allowed_by_request:
        edges.sort(key=lambda v: response_events[v]["cycle"])
    candidates = []
    confirmed_pairs = []
    for u, request in enumerate(request_events):
        options = [{"cycle": response_events[v]["cycle"],
                    "vector_id": response_events[v]["vector_id"]}
                   for v in allowed_by_request[u]]
        candidates.append({"request_cycle": request["cycle"],
                           "request_vector_id": request["vector_id"],
                           "candidates": options})
        if len(options) == 1:
            v = allowed_by_request[u][0]
            confirmed_pairs.append({
                "request_cycle": request["cycle"],
                "request_vector_id": request["vector_id"],
                "response_cycle": response_events[v]["cycle"],
                "response_vector_id": response_events[v]["vector_id"],
                "tag": request["tag"]})

    deadlines = spec["deadlines"]

    def is_late(u, v):
        latency = response_events[v]["cycle"] - request_events[u]["cycle"]
        return latency > deadlines[request_events[u]["class"]]

    def hungarian(cost_one):
        """Minimum-cost perfect assignment on an n x n zero/one matrix.

        Canonical Hungarian algorithm for a minimum square assignment
        (dual potentials with minimum-slack augmenting trees), O(n^3).  The
        cost matrix is materialized once: an edge the compatibility graph
        forbids carries an effectively infinite cost, while the guaranteed
        perfect matching on allowed edges keeps the optimum away from it.
        """
        size = n_left
        infinity = size + 1
        matrix = [[infinity] * size for _ in range(size)]
        allowed_lookup = [frozenset(edges) for edges in allowed_by_request]
        for u in range(size):
            for v in allowed_by_request[u]:
                matrix[u][v] = 1 if cost_one(u, v) else 0

        potential_u = [0] * (size + 1)
        potential_v = [0] * (size + 1)
        match_u = [0] * (size + 1)
        match_v = [0] * (size + 1)

        for i in range(1, size + 1):
            match_v[0] = i
            free_column = 0
            min_slack = [infinity] * (size + 1)
            used_column = [False] * (size + 1)
            parent_row = [0] * (size + 1)
            while match_v[free_column] != 0:
                used_column[free_column] = True
                row = match_v[free_column]
                row_cost = matrix[row - 1]
                pu = potential_u[row]
                delta = infinity
                next_column = 0
                for column in range(1, size + 1):
                    if used_column[column]:
                        continue
                    current = row_cost[column - 1] - pu - potential_v[column]
                    if current < min_slack[column]:
                        min_slack[column] = current
                        parent_row[column] = free_column
                    if min_slack[column] < delta:
                        delta = min_slack[column]
                        next_column = column
                for column in range(size + 1):
                    if used_column[column]:
                        potential_u[match_v[column]] += delta
                        potential_v[column] -= delta
                    else:
                        min_slack[column] -= delta
                free_column = next_column
            # Flip along the recorded alternating path.
            while free_column != 0:
                previous_column = parent_row[free_column]
                row = match_v[previous_column]
                match_u[row] = free_column
                match_v[free_column] = row
                free_column = previous_column

        assignment = [match_u[i] - 1 for i in range(1, size + 1)]
        total = sum(1 for u, v in enumerate(assignment) if cost_one(u, v))
        assert all(v in allowed_lookup[u] for u, v in enumerate(assignment))
        return assignment, total

    min_left, minimum_late = hungarian(is_late)
    # The second run minimizes the number of timely edges, which maximizes
    # the number of late edges in the same perfect-matching polytope.
    max_left, minimum_timely = hungarian(lambda u, v: not is_late(u, v))
    maximum_late = n_left - minimum_timely

    min_witness = _witness(min_left, request_events, response_events)
    max_witness = _witness(max_left, request_events, response_events)
    assert sum(1 for u, v in enumerate(min_left) if is_late(u, v)) == minimum_late
    assert sum(1 for u, v in enumerate(max_left) if is_late(u, v)) == maximum_late

    tardiness = []
    for u, request in enumerate(request_events):
        late_flags = {is_late(u, v) for v in allowed_by_request[u]}
        if late_flags == {False}:
            verdict = "always_timely"
        elif late_flags == {True}:
            verdict = "always_late"
        else:
            verdict = "either"
        tardiness.append({"request_cycle": request["cycle"],
                          "request_vector_id": request["vector_id"],
                          "verdict": verdict})

    budget = spec["late_budget"]
    if maximum_late <= budget:
        compliance = "within_budget"
    elif minimum_late > budget:
        compliance = "over_budget"
    else:
        compliance = "needs_review"

    return {
        "pairable": True,
        "candidates": candidates,
        "confirmed_pairs": confirmed_pairs,
        "tardiness": tardiness,
        "late_range": {"minimum": minimum_late, "maximum": maximum_late,
                       "minimum_witness": min_witness,
                       "maximum_witness": max_witness},
        "compliance": compliance,
    }


def run_audit(data):
    spec = compile_audit(data)
    result = replay(spec["gates"], spec["flops"], spec["outputs"],
                    spec["vectors"], spec["order"])
    trace = result["trace"]
    issues, request_events, response_events = extract_events(spec, trace)

    parameters = {"max_age": spec["max_age"], "deadlines": spec["deadlines"],
                  "late_budget": spec["late_budget"], "tag_width": spec["tag_width"]}
    summary_base = {"cycles": len(trace)}

    if issues:
        return {"schema_version": SCHEMA_VERSION, "status": "unobservable",
                "parameters": parameters, "observation_issues": issues,
                "summary": {**summary_base, "status": "unobservable",
                            "observation_issues": len(issues)}}

    analysis = _analyze_pairing(spec, request_events, response_events)
    request_table = [_public_event(event, True) for event in request_events]
    response_table = [_public_event(event, False) for event in response_events]

    if not analysis["pairable"]:
        return {"schema_version": SCHEMA_VERSION, "status": "unpairable",
                "parameters": parameters,
                "request_events": request_table,
                "response_events": response_table,
                "pairing": {"maximum_matching": analysis["maximum_matching"]},
                "summary": {**summary_base, "status": "unpairable",
                            "requests": len(request_events),
                            "responses": len(response_events),
                            "maximum_matching": analysis["maximum_matching"]}}

    budget = spec["late_budget"]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pairable",
        "parameters": parameters,
        "request_events": request_table,
        "response_events": response_table,
        "pairing": {"candidates": analysis["candidates"],
                    "confirmed_pairs": analysis["confirmed_pairs"],
                    "tardiness": analysis["tardiness"],
                    "late_range": analysis["late_range"]},
        "compliance": {"status": analysis["compliance"], "late_budget": budget},
        "summary": {**summary_base, "status": "pairable",
                    "requests": len(request_events),
                    "responses": len(response_events),
                    "confirmed_pairs": len(analysis["confirmed_pairs"]),
                    "minimum_late": analysis["late_range"]["minimum"],
                    "maximum_late": analysis["late_range"]["maximum"],
                    "compliance": analysis["compliance"]},
    }
