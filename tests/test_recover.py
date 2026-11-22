import itertools
import unittest

from logic_replay.gates import evaluate
from logic_replay.netlist import CONSTANTS
from logic_replay.recover import compile_recover, run_recover


def or_circuit():
    """q <- OR(q, a), initial 0, observes q."""
    return {"inputs": ["a"],
            "gates": [{"id": "nq", "op": "OR", "args": ["q", "a"]}],
            "flops": [{"id": "q", "d": "nq", "initial": "0"}],
            "outputs": ["q"]}


def xor_circuit():
    """q <- XOR(q, a), initial 0, observes q."""
    return {"inputs": ["a"],
            "gates": [{"id": "nq", "op": "XOR", "args": ["q", "a"]}],
            "flops": [{"id": "q", "d": "nq", "initial": "0"}],
            "outputs": ["q"]}


def rows(*cells):
    """cells: (id, inputs, observed); inputs None means the row omits a."""
    return [{"id": cid,
             "inputs": {} if given is None else dict(given),
             "observed": dict(observed)}
            for cid, given, observed in cells]


def windows(*ranges):
    return [{"id": wid, "from": start, "through": stop}
            for wid, start, stop in ranges]


def replay_row(spec, mapping, state):
    """Apply one completely specified input map to a flop-state tuple."""
    names = sorted(spec["flops"])
    values = {**CONSTANTS, **mapping}
    values.update(zip(names, state))
    for gate_id in spec["order"]:
        gate = spec["gates"][gate_id]
        values[gate_id] = evaluate(gate["op"],
                                   [values[arg] for arg in gate["args"]])
    observed = {name: values[name] for name in spec["outputs"]}
    next_state = tuple(values[spec["flops"][name]["d"]] for name in names)
    return observed, next_state


def brute_force(data):
    """Enumerate every complete explanation independently of the module.

    Returns full explanations as lists of mappings, plus the set of states
    entering each cycle that belong to at least one complete explanation.
    """
    if "windows" not in data:
        data = {**data, "windows": []}
    spec = compile_recover(data)
    inputs, names = spec["inputs"], sorted(spec["flops"])
    initial = tuple(spec["flops"][name]["initial"] for name in names)
    count = len(spec["rows"])
    options = []
    for index, row in enumerate(spec["rows"]):
        missing = spec["missing"][index]
        per_row = []
        for combo in itertools.product(("0", "1"), repeat=len(missing)):
            mapping = dict(row["inputs"])
            mapping.update(zip(missing, combo))
            per_row.append(mapping)
        options.append(per_row)

    explanations, entry_states = [], [set() for _ in range(count + 1)]
    prefix_ok = [False] * (count + 1)
    prefix_states = [set() for _ in range(count + 1)]

    def dfs(index, state, chosen, states):
        prefix_ok[index] = True
        prefix_states[index].add(state)
        if index == count:
            explanations.append((list(chosen), list(states)))
            return
        row = spec["rows"][index]
        for mapping in options[index]:
            observed, next_state = replay_row(spec, mapping, state)
            if all(observed[name] == want for name, want in row["observed"].items()):
                chosen.append({name: mapping[name] for name in spec["missing"][index]})
                dfs(index + 1, next_state, chosen, states + [next_state])
                chosen.pop()

    dfs(0, initial, [], [initial])
    for _mapping, states in explanations:
        for index, state in enumerate(states):
            entry_states[index].add(state)
    return {"explanations": explanations, "entry_states": entry_states,
            "prefix_ok": prefix_ok, "prefix_states": prefix_states,
            "spec": spec, "initial": initial}


def warmup_reaches(data, start_id, window_id, target):
    """Replay contiguous rows start_id..before-window_id from initial values."""
    spec = compile_recover(data)
    ids = [row["id"] for row in spec["rows"]]
    start, stop = ids.index(start_id), ids.index(window_id)
    names = sorted(spec["flops"])
    state = tuple(spec["flops"][name]["initial"] for name in names)
    for index in range(start, stop):
        row = spec["rows"][index]
        mapping = dict(row["inputs"])
        for name in spec["missing"][index]:
            # A warmup must be concrete; here the caller supplies only rows
            # whose omitted cells are uniquely recovered, so look them up.
            mapping[name] = RECOVERED[(row["id"], name)]
        _observed, state = replay_row(spec, mapping, state)
    return dict(zip(names, state)) == target


RECOVERED = {}


def recovered_map(result):
    found = {}
    for cycle in result["cycles"]:
        for cell in cycle["inputs"]:
            if cell["status"] != "recorded":
                found[(cycle["id"], cell["input"])] = cell["value"]
    return found


class RecoveryTests(unittest.TestCase):
    def scenario1(self, drop_sample=False):
        capture = rows(("t0", None, {}), ("t1", {"a": "0"}, {}),
                       ("t2", {"a": "0"}, {}),
                       ("t3", {"a": "1"}, {} if drop_sample else {"q": "1"}),
                       ("t4", {"a": "0"}, {}))
        return {**or_circuit(), "rows": capture,
                "windows": windows(("w2", "t2", "t2"), ("w4", "t4", "t4"))}

    def test_late_sample_uniquely_recovers_forcing_input(self):
        data = self.scenario1()
        result = run_recover(data)
        self.assertEqual(result["status"], "consistent")
        self.assertIsNone(result["first_contradiction"])
        self.assertEqual([row["id"] for row in result["cycles"]],
                         ["t0", "t1", "t2", "t3", "t4"])
        self.assertEqual(result["cycles"][0]["inputs"][0],
                         {"input": "a", "status": "recovered", "value": "1"})
        self.assertEqual(result["cycles"][3]["inputs"][0],
                         {"input": "a", "status": "recorded", "value": "1"})
        br = brute_force(data)
        self.assertEqual(len(br["explanations"]), 1)
        self.assertEqual(br["explanations"][0][0][0], {"a": "1"})
        self.assertEqual(br["entry_states"][2], {("1",)})

    def test_window_handoff_uses_shortest_warmup(self):
        data = self.scenario1()
        result = run_recover(data)
        global RECOVERED
        RECOVERED = recovered_map(result)
        windows_out = {row["id"]: row for row in result["windows"]}
        w2 = windows_out["w2"]
        self.assertEqual(w2["status"], "handoff")
        self.assertEqual(w2["entry_state"], {"q": "1"})
        self.assertEqual(w2["warmup_cycle_ids"], ["t0", "t1"])
        self.assertEqual(w2["warmup_length"], 2)
        self.assertEqual(w2["window_cycle_ids"], ["t2"])
        # t1 alone cannot reach q=1 (initial q=0, a=0 keeps it 0), while
        # t0..t1 does: the sample at t3 retroactively forces t0's a to "1".
        self.assertTrue(warmup_reaches(data, "t0", "t2", {"q": "1"}))
        self.assertFalse(warmup_reaches(data, "t1", "t2", {"q": "1"}))
        w4 = windows_out["w4"]
        self.assertEqual(w4["status"], "handoff")
        self.assertEqual(w4["entry_state"], {"q": "1"})
        self.assertEqual(w4["warmup_cycle_ids"], ["t3"])
        self.assertEqual(w4["warmup_length"], 1)
        # The warmup row carries no expectation even though history sampled
        # q=1 there; on restart t3's pre-edge q is 0.
        package = w4["replay_package"]
        self.assertEqual([vector["id"] for vector in package["vectors"]], ["t3", "t4"])
        self.assertEqual(package["vectors"][0]["expect"], {})
        self.assertEqual(package["vectors"][1]["expect"], {})

    def test_deleting_late_sample_blocks_earlier_window_only(self):
        data = self.scenario1(drop_sample=True)
        result = run_recover(data)
        windows_out = {row["id"]: row for row in result["windows"]}
        w2 = windows_out["w2"]
        self.assertEqual(w2["status"], "blocked")
        self.assertEqual(w2["blocking_reason"], "entry_state_ambiguity")
        self.assertEqual(w2["entry_states"], [{"q": "0"}, {"q": "1"}])
        self.assertNotIn("replay_package", w2)
        w4 = windows_out["w4"]
        self.assertEqual(w4["status"], "handoff")
        self.assertEqual(w4["warmup_cycle_ids"], ["t3"])
        self.assertEqual(w4["warmup_length"], 1)
        cell = result["cycles"][0]["inputs"][0]
        self.assertEqual(cell["status"], "ambiguous")
        self.assertEqual(cell["candidates"], ["0", "1"])

    def test_observed_output_is_never_fabricated(self):
        data = {**or_circuit(),
                "rows": rows(("t0", {"a": "1"}, {}),
                             ("t1", {"a": "0"}, {"q": "1"}),
                             ("t2", {"a": "0"}, {})),
                "windows": windows(("w", "t1", "t2"))}
        result = run_recover(data)
        window = result["windows"][0]
        self.assertEqual(window["status"], "handoff")
        self.assertEqual(window["warmup_cycle_ids"], ["t0"])
        package = window["replay_package"]
        self.assertEqual([vector["id"] for vector in package["vectors"]],
                         ["t0", "t1", "t2"])
        # Warmup expectation empty; window rows copy only the original sample.
        self.assertEqual([vector["expect"] for vector in package["vectors"]],
                         [{}, {"q": "1"}, {}])

    def test_xor_ambiguous_warmup_blocks_handoff(self):
        # Two omitted cells each have candidates {0,1} but must differ; the
        # entry state is still unique.  No single concrete warmup exists.
        data = {**xor_circuit(),
                "rows": rows(("x0", None, {}), ("x1", None, {}),
                             ("x2", {"a": "1"}, {"q": "1"})),
                "windows": windows(("wx", "x2", "x2"))}
        result = run_recover(data)
        self.assertEqual(result["status"], "consistent")
        self.assertEqual(result["cycles"][0]["inputs"][0]["candidates"], ["0", "1"])
        self.assertEqual(result["cycles"][1]["inputs"][0]["candidates"], ["0", "1"])
        window = result["windows"][0]
        self.assertEqual(window["status"], "blocked")
        self.assertEqual(window["blocking_reason"], "no_eligible_warmup")
        self.assertNotIn("replay_package", window)
        br = brute_force(data)
        self.assertEqual(br["entry_states"][2], {("1",)})
        for _mapping, states in br["explanations"]:
            self.assertEqual(states[2], ("1",))
        # The candidate sets are real: each cell values take different bits.
        seen = {(exp[0][0]["a"], exp[0][1]["a"]) for exp in br["explanations"]}
        self.assertEqual(seen, {("0", "1"), ("1", "0")})

    def test_recorded_xor_neighbour_pins_the_other_cell(self):
        # Recording x1's a=1 forces x0's a to "0" (they must differ); the
        # warmup is therefore concrete and handoff succeeds.
        data = {**xor_circuit(),
                "rows": rows(("x0", None, {}), ("x1", {"a": "1"}, {}),
                             ("x2", {"a": "1"}, {"q": "1"})),
                "windows": windows(("wx", "x2", "x2"))}
        result = run_recover(data)
        self.assertEqual(result["cycles"][0]["inputs"][0],
                         {"input": "a", "status": "recovered", "value": "0"})
        window = result["windows"][0]
        self.assertEqual(window["status"], "handoff")
        # One recorded row suffices to reach q=1 from initial q=0.
        self.assertEqual(window["warmup_cycle_ids"], ["x1"])
        self.assertEqual(window["entry_state"], {"q": "1"})

    def test_many_omissions_with_collapsing_state_stays_legal(self):
        circuit = {"inputs": ["a", "b"],
                   "gates": [],
                   "flops": [{"id": "q", "d": "q", "initial": "0"}],
                   "outputs": ["q"]}
        capture = rows(*[(f"r{i}", None, {}) for i in range(60)])
        capture[50]["inputs"] = {"a": "0", "b": "0"}
        capture[51]["inputs"] = {"a": "0", "b": "0"}
        capture[52]["inputs"] = {"a": "0", "b": "0"}
        data = {**circuit, "rows": capture,
                "windows": windows(("w", "r50", "r52"))}
        result = run_recover(data)
        self.assertEqual(result["status"], "consistent")
        self.assertEqual(len(result["cycles"]), 60)
        for cycle in result["cycles"]:
            if cycle["id"] in ("r50", "r51", "r52"):
                continue
            for cell in cycle["inputs"]:
                self.assertEqual(cell["status"], "ambiguous")
                self.assertEqual(cell["candidates"], ["0", "1"])
        self.assertEqual(result["summary"]["ambiguous_input_cells"], 114)
        # Entry state is nonetheless unique (q stays 0), so handoff works
        # with zero warmup.
        window = result["windows"][0]
        self.assertEqual(window["status"], "handoff")
        self.assertEqual(window["warmup_length"], 0)
        self.assertEqual(window["entry_state"], {"q": "0"})
        # The ambiguity is genuine: many fully different stimulus histories
        # collapse into the same state, so the run must not reject them.
        short = brute_force({**circuit,
                             "rows": rows(*[(f"s{i}", None, {}) for i in range(6)])})
        self.assertEqual(len(short["explanations"]), 4 ** 6)
        self.assertEqual(short["entry_states"][6], {("0",)})

    def test_first_contradiction_cycle_and_blocked_windows(self):
        circuit = xor_circuit()
        capture = rows(("t0", {"a": "1"}, {"q": "0"}),
                       ("t1", {"a": "1"}, {"q": "1"}),
                       ("t2", {"a": "0"}, {"q": "1"}),
                       ("t3", {"a": "0"}, {}))
        data = {**circuit, "rows": capture,
                "windows": windows(("w0", "t0", "t0"), ("w3", "t3", "t3"))}
        result = run_recover(data)
        self.assertEqual(result["status"], "contradiction")
        self.assertEqual(result["first_contradiction"], "t2")
        self.assertEqual(result["first_contradiction_index"], 2)
        self.assertEqual(result["cycles"], [])
        for window in result["windows"]:
            self.assertEqual(window["status"], "blocked")
            self.assertEqual(window["blocking_reason"], "capture_contradiction")
            self.assertNotIn("replay_package", window)
        self.assertEqual(result["summary"]["handoffs"], 0)
        # Independent prefix enumeration: t0/t1 satisfy, t2 fails.
        br = brute_force({**circuit,
                          "rows": rows(("t0", {"a": "1"}, {"q": "0"}),
                                       ("t1", {"a": "1"}, {"q": "1"}),
                                       ("t2", {"a": "0"}, {"q": "1"}))})
        self.assertEqual(br["explanations"], [])
        self.assertEqual(br["prefix_ok"], [True, True, True, False])

    def test_explicit_unknown_input_is_a_literal_not_a_gap(self):
        # AND(X, 0) is 0 under three-valued logic, so the literal X input
        # leaves q unchanged and is reported exactly as recorded.
        circuit = {"inputs": ["a", "b"],
                   "gates": [{"id": "m", "op": "AND", "args": ["a", "b"]},
                             {"id": "nq", "op": "OR", "args": ["q", "m"]}],
                   "flops": [{"id": "q", "d": "nq", "initial": "0"}],
                   "outputs": ["q"]}
        data = {**circuit,
                "rows": rows(("t0", {"a": "X", "b": "0"}, {}),
                             ("t1", {"a": "1", "b": "1"}, {"q": "0"}),
                             ("t2", {"a": "0", "b": "0"}, {})),
                "windows": windows(("w", "t2", "t2"))}
        result = run_recover(data)
        self.assertEqual(result["cycles"][0]["inputs"],
                         [{"input": "a", "status": "recorded", "value": "X"},
                          {"input": "b", "status": "recorded", "value": "0"}])
        # The X input is not decomposed: q stayed 0 after t0, so the t1
        # sample is satisfied by m=0, and q becomes 1 for the window.
        br = brute_force(data)
        self.assertEqual(br["entry_states"][2], {("1",)})
        self.assertEqual(len(br["explanations"]), 1)
        window = result["windows"][0]
        self.assertEqual(window["status"], "handoff")
        self.assertEqual(window["entry_state"], {"q": "1"})
        # t1 alone replays from initial values to q=1.
        self.assertEqual(window["warmup_cycle_ids"], ["t1"])

    def test_observed_unknown_must_match_exactly(self):
        circuit = {"inputs": ["unused"],
                   "gates": [{"id": "hold", "op": "BUF", "args": ["q"]}],
                   "flops": [{"id": "q", "d": "hold", "initial": "X"}],
                   "outputs": ["q"]}
        ok = {**circuit,
              "rows": rows(("t0", {"unused": "0"}, {"q": "X"}),
                           ("t1", {"unused": "0"}, {})),
              "windows": windows()}
        self.assertEqual(run_recover(ok)["status"], "consistent")
        bad = {**circuit,
               "rows": rows(("t0", {"unused": "0"}, {"q": "0"})),
               "windows": windows(("w", "t0", "t0"))}
        result = run_recover(bad)
        self.assertEqual(result["status"], "contradiction")
        self.assertEqual(result["first_contradiction"], "t0")

    def test_zero_warmup_when_initial_state_already_matches(self):
        data = {**or_circuit(),
                "rows": rows(("t0", {"a": "0"}, {"q": "0"}),
                             ("t1", {"a": "0"}, {}),
                             ("t2", {"a": "1"}, {})),
                "windows": windows(("w", "t1", "t2"))}
        result = run_recover(data)
        window = result["windows"][0]
        self.assertEqual(window["status"], "handoff")
        self.assertEqual(window["warmup_length"], 0)
        self.assertEqual(window["warmup_cycle_ids"], [])
        self.assertEqual(window["entry_state"], {"q": "0"})
        package = window["replay_package"]
        self.assertEqual([vector["id"] for vector in package["vectors"]], ["t1", "t2"])
        self.assertEqual(package["flops"][0]["initial"], "0")

    def test_two_flops_update_simultaneously(self):
        # q1 <- a, q2 <- q1.  Both flops sample their pre-edge source: after
        # t1, q1=1 and q2=0 (the old q1), so the window entry needs both rows.
        circuit = {"inputs": ["a"],
                   "gates": [{"id": "d1", "op": "BUF", "args": ["a"]},
                             {"id": "d2", "op": "BUF", "args": ["q1"]}],
                   "flops": [{"id": "q1", "d": "d1", "initial": "0"},
                             {"id": "q2", "d": "d2", "initial": "0"}],
                   "outputs": ["q1", "q2"]}
        data = {**circuit,
                "rows": rows(("t0", {"a": "1"}, {"q1": "0", "q2": "0"}),
                             ("t1", {"a": "1"}, {"q1": "1", "q2": "0"}),
                             ("t2", {"a": "0"}, {"q1": "1", "q2": "1"}),
                             ("t3", {"a": "0"}, {})),
                "windows": windows(("w", "t3", "t3"))}
        result = run_recover(data)
        # t2's sample pins the entry: q1=0, q2=1 after the t2 edge.
        self.assertEqual(result["windows"][0]["entry_state"],
                         {"q1": "0", "q2": "1"})
        # Starting from initial values, t1..t2 reaches the entry; t2 alone
        # does not (it would leave q1=0,q2=0), and t0..t2 is longer.
        self.assertEqual(result["windows"][0]["warmup_cycle_ids"], ["t1", "t2"])
        package = result["windows"][0]["replay_package"]
        self.assertEqual(package["flops"],
                         [{"id": "q1", "d": "d1", "initial": "0"},
                          {"id": "q2", "d": "d2", "initial": "0"}])
        br = brute_force(data)
        self.assertEqual(br["entry_states"][1], {("1", "0")})

    def test_window_results_follow_input_array_order(self):
        data = {**or_circuit(),
                "rows": rows(("z0", {"a": "0"}, {}), ("a1", {"a": "1"}, {}),
                             ("m2", {"a": "0"}, {})),
                "windows": windows(("wB", "a1", "m2"), ("wA", "z0", "z0"))}
        result = run_recover(data)
        self.assertEqual([row["id"] for row in result["windows"]], ["wB", "wA"])
        self.assertEqual([row["id"] for row in result["cycles"]], ["z0", "a1", "m2"])

    def test_overlapping_windows_are_independent(self):
        data = self.scenario1()
        data["windows"] = windows(("wA", "t2", "t3"), ("wB", "t2", "t4"))
        result = run_recover(data)
        self.assertEqual([row["status"] for row in result["windows"]],
                         ["handoff", "handoff"])


class RecoverValidationTests(unittest.TestCase):
    def assert_invalid(self, data, pattern):
        with self.assertRaisesRegex(ValueError, pattern):
            run_recover(data)

    def test_bad_window_references(self):
        data = {**or_circuit(),
                "rows": rows(("t0", {"a": "0"}, {}), ("t1", {"a": "0"}, {})),
                "windows": windows(("w", "t0", "nope"))}
        self.assert_invalid(data, "unknown cycle")
        data = {**or_circuit(),
                "rows": rows(("t0", {"a": "0"}, {}), ("t1", {"a": "0"}, {})),
                "windows": windows(("w", "t1", "t0"))}
        self.assert_invalid(data, "capture order")

    def test_duplicate_ids(self):
        data = {**or_circuit(),
                "rows": rows(("t0", {"a": "0"}, {}), ("t0", {"a": "0"}, {})),
                "windows": windows()}
        self.assert_invalid(data, "duplicate row ID")
        data = {**or_circuit(),
                "rows": rows(("t0", {"a": "0"}, {}), ("t1", {"a": "0"}, {})),
                "windows": windows(("w", "t0", "t0"), ("w", "t1", "t1"))}
        self.assert_invalid(data, "duplicate window ID")

    def test_unknown_fields(self):
        data = {**or_circuit(),
                "rows": rows(("t0", {"a": "0"}, {})),
                "windows": windows(), "note": "extra"}
        self.assert_invalid(data, "unknown")
        data = {**or_circuit(),
                "rows": [{"id": "t0", "inputs": {"a": "0"}, "observed": {},
                          "cycle": 0}],
                "windows": windows()}
        self.assert_invalid(data, "unknown")
        data = {**or_circuit(),
                "rows": rows(("t0", {"a": "0"}, {})),
                "windows": [{"id": "w", "from": "t0", "through": "t0",
                             "note": "x"}]}
        self.assert_invalid(data, "unknown")

    def test_bounds_and_values(self):
        data = {**or_circuit(),
                "rows": [{"id": f"r{i}", "inputs": {"a": "0"}, "observed": {}}
                         for i in range(300)],
                "windows": windows()}
        self.assert_invalid(data, "rows")
        data = {**or_circuit(),
                "rows": rows(("t0", {"a": "Q"}, {})), "windows": windows()}
        self.assert_invalid(data, "row.input value")
        data = {**or_circuit(),
                "rows": rows(("t0", {"a": "0"}, {"q": "*"})), "windows": windows()}
        self.assert_invalid(data, "row.observed value")
        data = {**or_circuit(),
                "rows": rows(("t0", {"a": 0}, {})), "windows": windows()}
        self.assert_invalid(data, "row.input value")
        data = {**or_circuit(),
                "rows": rows(("t0", {"a": "0"}, {"ghost": "0"})),
                "windows": windows()}
        self.assert_invalid(data, "outputs")
        data = {**or_circuit(),
                "rows": rows(("t0", {"ghost": "0"}, {})), "windows": windows()}
        self.assert_invalid(data, "inputs")
        data = {**or_circuit(),
                "rows": rows(("t0", {"a": "0"}, {})), "windows": windows()}
        data["inputs"] = ["a", "b", "c", "d", "e"]
        data["gates"][0]["args"] = ["q", "a"]
        with self.assertRaisesRegex(ValueError, "at most 3 inputs"):
            run_recover(data)
        data = {**or_circuit(),
                "rows": rows(("t0", {"a": "0"}, {})),
                "windows": [{"id": f"w{i}", "from": "t0", "through": "t0"}
                            for i in range(33)]}
        self.assert_invalid(data, "window")

    def test_more_than_six_flops_rejected(self):
        flops = [{"id": f"q{i}", "d": f"q{i}", "initial": "0"} for i in range(7)]
        circuit = {"inputs": ["unused"], "gates": [], "flops": flops,
                   "outputs": ["q0"]}
        data = {**circuit,
                "rows": rows(("t0", {"unused": "0"}, {})), "windows": windows()}
        self.assert_invalid(data, "at most 6 flops")

    def test_netlist_rules_still_apply(self):
        data = {**or_circuit(),
                "gates": [{"id": "nq", "op": "BUF", "args": ["nq"]}],
                "rows": rows(("t0", {"a": "0"}, {})), "windows": windows()}
        self.assert_invalid(data, "cycle")
        data = {**or_circuit(), "rows": rows(("t0", {"a": "0"}, {})),
                "windows": windows(), "vectors": []}
        self.assert_invalid(data, "unknown")


if __name__ == "__main__":
    unittest.main()


class ReplayPackageTests(unittest.TestCase):
    def test_package_replays_through_v1_entry_point(self):
        from logic_replay.service import run

        data = {**or_circuit(),
                "rows": rows(("t0", None, {}), ("t1", {"a": "0"}, {}),
                             ("t2", {"a": "0"}, {}), ("t3", {"a": "1"}, {"q": "1"}),
                             ("t4", {"a": "0"}, {"q": "1"})),
                "windows": windows(("w4", "t4", "t4"))}
        result = run_recover(data)
        window = result["windows"][0]
        self.assertEqual(window["status"], "handoff")
        self.assertEqual(window["warmup_cycle_ids"], ["t3"])
        package = window["replay_package"]
        replayed = run(package)
        self.assertTrue(replayed["summary"]["all_expected_match"])
        self.assertEqual(replayed["summary"]["comparisons"], 1)
        first = len(window["warmup_cycle_ids"])
        # The window's first cycle pre-edge state equals the recovered entry.
        self.assertEqual(replayed["trace"][first]["state_before_edge"],
                         window["entry_state"])
        # Only the original t4 sample is compared; t3 is a warmup row whose
        # historical sample must not become an expectation.
        self.assertEqual([vector["expect"] for vector in package["vectors"]],
                         [{}, {"q": "1"}])
        self.assertEqual(replayed["trace"][0]["outputs_before_edge"], {"q": "0"})
        self.assertEqual(window["entry_state"], {"q": "1"})

    def test_package_is_self_contained(self):
        data = RecoveryTests.scenario1(self)
        package = run_recover(data)["windows"][0]["replay_package"]
        # It carries the original netlist and only the selected vectors.
        self.assertEqual(package["inputs"], ["a"])
        self.assertEqual(package["flops"],
                         [{"id": "q", "d": "nq", "initial": "0"}])
        self.assertEqual([vector["id"] for vector in package["vectors"]],
                         ["t0", "t1", "t2"])


class ContradictionExitTests(unittest.TestCase):
    def test_contradiction_is_a_business_result(self):
        data = {**xor_circuit(),
                "rows": rows(("a0", {"a": "1"}, {"q": "0"}),
                             ("b1", {"a": "1"}, {"q": "0"})),
                "windows": windows(("v", "a0", "a0"), ("z", "b1", "b1"))}
        result = run_recover(data)
        self.assertEqual(result["status"], "contradiction")
        self.assertEqual(result["first_contradiction"], "b1")
        for window in result["windows"]:
            self.assertEqual(window["status"], "blocked")
            self.assertEqual(window["blocking_reason"], "capture_contradiction")


class DeterminismTests(unittest.TestCase):
    def test_object_key_order_does_not_change_report(self):
        data = RecoveryTests.scenario1(self)
        reordered = {
            "windows": data["windows"],
            "outputs": data["outputs"],
            "flops": data["flops"],
            "gates": data["gates"],
            "inputs": data["inputs"],
            "rows": [{"observed": row["observed"], "inputs": row["inputs"],
                      "id": row["id"]} for row in data["rows"]],
        }
        reordered["flops"] = [{"initial": f["initial"], "d": f["d"], "id": f["id"]}
                              for f in reordered["flops"]]
        reordered["windows"] = [{"through": w["through"], "from": w["from"],
                                 "id": w["id"]} for w in reordered["windows"]]
        self.assertEqual(run_recover(data), run_recover(reordered))

    def test_row_and_window_order_drives_results(self):
        # Reversing the array is legal as long as windows follow the new
        # order; results are reported in array order, never sorted by ID.
        data = {**or_circuit(),
                "rows": rows(("z9", {"a": "0"}, {}), ("y8", {"a": "1"}, {}),
                             ("x7", {"a": "0"}, {})),
                "windows": windows(("w", "y8", "x7"))}
        result = run_recover(data)
        self.assertEqual([row["id"] for row in result["cycles"]], ["z9", "y8", "x7"])
        data["rows"] = list(reversed(data["rows"]))
        with self.assertRaisesRegex(ValueError, "capture order"):
            run_recover(data)


class GlobalExplanationTests(unittest.TestCase):
    def test_candidate_requires_matching_a_later_sample(self):
        # OR: once q latches to 1 it never drops.  At t0 the omitted a could
        # locally be either bit, but the t2 sample q=0 forces it to "0" using
        # a constraint from two cycles later.
        data = {**or_circuit(),
                "rows": rows(("t0", None, {}), ("t1", {"a": "0"}, {}),
                             ("t2", {"a": "0"}, {"q": "0"}),
                             ("t3", {"a": "0"}, {})),
                "windows": windows(("w", "t3", "t3"))}
        result = run_recover(data)
        self.assertEqual(result["cycles"][0]["inputs"][0],
                         {"input": "a", "status": "recovered", "value": "0"})
        br = brute_force(data)
        self.assertEqual({exp[0][0]["a"] for exp in br["explanations"]}, {"0"})
        self.assertEqual(window_entry(result, "w"), {"q": "0"})

    def test_contradiction_needs_only_prefix(self):
        # Each adjacent pair is satisfiable, but the whole prefix is not once
        # the t2 sample is added: t0 -> q=1, t1 -> q=0, so t1 cannot sample 1.
        data = {**xor_circuit(),
                "rows": rows(("t0", {"a": "1"}, {"q": "0"}),
                             ("t1", {"a": "1"}, {"q": "0"}),
                             ("t2", {"a": "0"}, {})),
                "windows": windows()}
        result = run_recover(data)
        # t0 alone is fine (initial q=0); the t1 sample cannot hold because
        # t0's a=1 already flipped q to 1.
        self.assertEqual(result["first_contradiction"], "t1")
        br = brute_force(data)
        self.assertEqual(br["prefix_ok"], [True, True, False, False])


def window_entry(result, name):
    for window in result["windows"]:
        if window["id"] == name:
            return window["entry_state"]
    raise AssertionError(name)


class PackageExpectationTests(unittest.TestCase):
    def test_only_sampled_outputs_become_expectations(self):
        from logic_replay.service import run
        # Two outputs; only q is sampled once in the window, g is never.
        circuit = {"inputs": ["a"],
                   "gates": [{"id": "nq", "op": "OR", "args": ["q", "a"]},
                             {"id": "g", "op": "NOT", "args": ["a"]}],
                   "flops": [{"id": "q", "d": "nq", "initial": "0"}],
                   "outputs": ["q", "g"]}
        data = {**circuit,
                "rows": rows(("t0", {"a": "1"}, {}),
                             ("t1", {"a": "0"}, {"q": "1"}),
                             ("t2", {"a": "1"}, {"q": "1"})),
                "windows": windows(("w", "t1", "t2"))}
        result = run_recover(data)
        window = result["windows"][0]
        self.assertEqual(window["status"], "handoff")
        package = window["replay_package"]
        self.assertEqual(package["outputs"], ["q", "g"])
        self.assertEqual([vector["expect"] for vector in package["vectors"]],
                         [{}, {"q": "1"}, {"q": "1"}])
        replayed = run(package)
        self.assertTrue(replayed["summary"]["all_expected_match"])
        self.assertEqual(replayed["summary"]["comparisons"], 2)
        self.assertEqual(replayed["trace"][1]["state_before_edge"], window["entry_state"])


class BoundaryTests(unittest.TestCase):
    def test_256_cycles_allowed_and_257_rejected(self):
        circuit = {"inputs": ["a"],
                   "gates": [{"id": "nq", "op": "OR", "args": ["q", "a"]}],
                   "flops": [{"id": "q", "d": "nq", "initial": "0"}],
                   "outputs": ["q"]}
        for count, ok in ((256, True), (257, False)):
            capture = [{"id": f"r{i}", "inputs": {"a": "0"}, "observed": {}}
                       for i in range(count)]
            data = {**circuit, "rows": capture, "windows": windows()}
            if ok:
                self.assertEqual(run_recover(data)["status"], "consistent")
            else:
                with self.assertRaisesRegex(ValueError, "rows"):
                    run_recover(data)

    def test_six_flops_allowed(self):
        flops = [{"id": f"q{i}", "d": f"q{i}", "initial": "0"} for i in range(6)]
        circuit = {"inputs": ["unused"], "gates": [], "flops": flops,
                   "outputs": ["q0"]}
        data = {**circuit, "rows": rows(("t0", {"unused": "0"}, {}),
                                        ("t1", {"unused": "0"}, {})),
                "windows": windows(("w", "t1", "t1"))}
        result = run_recover(data)
        self.assertEqual(result["status"], "consistent")
        self.assertEqual(result["windows"][0]["warmup_length"], 0)


class BlockingOrderTests(unittest.TestCase):
    def test_window_input_ambiguity_wins_over_entry_ambiguity(self):
        # Window spans an omitted row whose cell is ambiguous, while the
        # entry state is also ambiguous; the input condition must win.
        data = {**or_circuit(),
                "rows": rows(("t0", None, {}), ("t1", {"a": "0"}, {}),
                             ("t2", None, {})),
                "windows": windows(("w", "t2", "t2"))}
        result = run_recover(data)
        window = result["windows"][0]
        self.assertEqual(window["status"], "blocked")
        self.assertEqual(window["blocking_reason"], "window_input_ambiguity")
        self.assertEqual(window["ambiguous_cells"],
                         [{"cycle": "t2", "input": "a", "candidates": ["0", "1"]}])

    def test_entry_ambiguity_wins_over_warmup_failure(self):
        # Window inputs are recorded, but t0 is omitted so the entry state
        # can be q=0 or q=1.  Entry ambiguity is reported before any warmup
        # search, and the warmup over t0 could never be concrete anyway.
        data = {**or_circuit(),
                "rows": rows(("t0", None, {}), ("t1", {"a": "0"}, {})),
                "windows": windows(("w", "t1", "t1"))}
        result = run_recover(data)
        window = result["windows"][0]
        self.assertEqual(window["status"], "blocked")
        self.assertEqual(window["blocking_reason"], "entry_state_ambiguity")
        self.assertEqual(window["entry_states"], [{"q": "0"}, {"q": "1"}])

    def test_unique_entry_with_unreachable_warmup(self):
        # Both pre-window rows are omitted, so any warmup would have to
        # choose concrete stimuli that the capture never established even
        # though the entry itself is unique (observation forces q=1).
        data = {**xor_circuit(),
                "rows": rows(("t0", None, {}), ("t1", None, {}),
                             ("t2", {"a": "1"}, {"q": "1"})),
                "windows": windows(("w", "t2", "t2"))}
        result = run_recover(data)
        window = result["windows"][0]
        self.assertEqual(window["status"], "blocked")
        self.assertEqual(window["blocking_reason"], "no_eligible_warmup")
        self.assertEqual(window["entry_state"], {"q": "1"})
