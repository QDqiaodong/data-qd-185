import json
import random
import unittest
from logic_replay.campaign import (collect_witnesses, compile_campaign,
                                   run_campaign, simulate_fragment)


def prime_observe(a_value, budget=3, reset_cost=1):
    """g=BUF(a), q.D=g, q.initial=1, fault g stuck at 0."""
    return {
        "inputs": ["a"],
        "gates": [{"id": "g", "op": "BUF", "args": ["a"]}],
        "flops": [{"id": "q", "d": "g", "initial": "1"}],
        "outputs": ["q"],
        "fragments": [
            {"id": "prime", "vectors": [
                {"id": "p0", "inputs": {"a": a_value}, "expect": {"q": "*"}}]},
            {"id": "observe", "vectors": [
                {"id": "o0", "inputs": {"a": "1"}, "expect": {"q": "1"}}]},
        ],
        "faults": [{"gate": "g", "stuck_at": "0"}],
        "cycle_budget": budget,
        "reset_cost": reset_cost,
    }


def multi_fault():
    """Stateless AND/OR/BUF chain with three independent faults."""
    return {
        "inputs": ["a", "b"],
        "gates": [{"id": "g1", "op": "AND", "args": ["a", "b"]},
                  {"id": "g2", "op": "OR", "args": ["a", "b"]},
                  {"id": "g3", "op": "BUF", "args": ["g1"]}],
        "flops": [],
        "outputs": ["g1", "g2", "g3"],
        "fragments": [
            {"id": "combo", "vectors": [
                {"id": "v1", "inputs": {"a": "1", "b": "1"},
                 "expect": {"g1": "1", "g2": "1", "g3": "1"}},
                {"id": "v2", "inputs": {"a": "0", "b": "0"},
                 "expect": {"g1": "0", "g2": "0"}}]},
            {"id": "kick", "vectors": [
                {"id": "k1", "inputs": {"a": "1", "b": "1"}, "expect": {"g1": "1"}}]},
            {"id": "trip", "vectors": [
                {"id": "t1", "inputs": {"a": "0", "b": "0"}, "expect": {"g2": "0"}}]},
            {"id": "low", "vectors": [
                {"id": "l1", "inputs": {"a": "0", "b": "1"}, "expect": {"g3": "0"}}]},
        ],
        "faults": [{"gate": "g1", "stuck_at": "0"},
                   {"gate": "g2", "stuck_at": "1"},
                   {"gate": "g3", "stuck_at": "1"}],
        "cycle_budget": 2,
        "reset_cost": 1,
    }


def unknown_masking():
    """Faulty X and expect-X must not count as detections."""
    return {
        "inputs": ["a", "c"],
        "gates": [{"id": "g", "op": "BUF", "args": ["a"]},
                  {"id": "h", "op": "OR", "args": ["g", "c"]}],
        "flops": [],
        "outputs": ["h"],
        "fragments": [
            {"id": "px", "vectors": [
                {"id": "w1", "inputs": {"a": "1", "c": "X"}, "expect": {"h": "1"}},
                {"id": "w2", "inputs": {"a": "X", "c": "0"}, "expect": {"h": "X"}}]},
        ],
        "faults": [{"gate": "g", "stuck_at": "0"},
                   {"gate": "h", "stuck_at": "0"}],
        "cycle_budget": 4,
        "reset_cost": 1,
    }


def reset_needed():
    """Only load + reset + probe detects both faults."""
    return {
        "inputs": ["a"],
        "gates": [{"id": "g", "op": "BUF", "args": ["a"]},
                  {"id": "h", "op": "NOT", "args": ["q"]}],
        "flops": [{"id": "q", "d": "g", "initial": "0"}],
        "outputs": ["q", "g", "h"],
        "fragments": [
            {"id": "load", "vectors": [
                {"id": "ld", "inputs": {"a": "1"}, "expect": {"q": "0", "g": "1"}}]},
            {"id": "probe", "vectors": [
                {"id": "pr", "inputs": {"a": "1"}, "expect": {"q": "0", "h": "1"}}]},
        ],
        "faults": [{"gate": "g", "stuck_at": "0"},
                   {"gate": "h", "stuck_at": "0"}],
        "cycle_budget": 3,
        "reset_cost": 1,
    }


def steps(result):
    return [(row["fragment_id"], row["reset_before"]) for row in result["schedule"]]


def brute_force(data):
    """Independent from-scratch enumeration of the whole schedule space."""
    spec = compile_campaign(data)
    gates, flops = spec["gates"], spec["flops"]
    outputs, order = spec["outputs"], spec["order"]
    fragments, faults = spec["fragments"], spec["faults"]
    budget, reset_cost = spec["cycle_budget"], spec["reset_cost"]
    initial = tuple(flops[name]["initial"] for name in sorted(flops))

    def evaluate(plan):
        ref_state = initial
        fault_states = [initial] * len(faults)
        detected, witnesses = set(), {}
        for fid, reset in plan:
            if reset:
                ref_state = initial
                fault_states = [initial] * len(faults)
            vectors = fragments[fid]["vectors"]
            ref_state, ref_obs = simulate_fragment(gates, flops, outputs, order,
                                                   vectors, ref_state, None)
            fault_obs = []
            for index, fault in enumerate(faults):
                fault_states[index], obs = simulate_fragment(
                    gates, flops, outputs, order, vectors, fault_states[index], fault)
                fault_obs.append(obs)
            for cycle, (vector_id, vector) in enumerate(vectors.items()):
                for name, want in vector["expect"].items():
                    if want != "*" and ref_obs[cycle][name] != want:
                        return False, frozenset(), {}
                points = sorted(name for name, want in vector["expect"].items()
                                if want in ("0", "1"))
                for index in range(len(faults)):
                    for name in points:
                        ref_value = ref_obs[cycle][name]
                        faulty_value = fault_obs[index][cycle][name]
                        if (ref_value in ("0", "1") and faulty_value in ("0", "1")
                                and ref_value != faulty_value):
                            detected.add(index)
                            if index not in witnesses:
                                witnesses[index] = {
                                    "fragment_id": fid, "vector_id": vector_id,
                                    "output": name, "reference_value": ref_value,
                                    "faulty_value": faulty_value}
                            break
        return True, frozenset(detected), witnesses

    best = [None]

    def rec(remaining, cost, plan):
        valid, detected, witnesses = evaluate(plan)
        if valid:
            key = (-len(detected), cost, tuple(plan))
            if best[0] is None or key < best[0][0]:
                best[0] = (key, tuple(plan), detected, witnesses)
        else:
            return  # extensions stay reference-invalid
        for fid in sorted(fragments):
            if fid not in remaining:
                continue
            for reset in ((False,) if not plan else (False, True)):
                new_cost = cost + len(fragments[fid]["vectors"]) + (reset_cost if reset else 0)
                if new_cost <= budget:
                    rec(remaining - {fid}, new_cost, plan + [(fid, reset)])

    rec(set(fragments), 0, [])
    return best[0]


def random_campaign(rng):
    inputs = ["a", "b"]
    gates = []
    signals = list(inputs)
    for i in range(rng.randint(1, 4)):
        op = rng.choice(["BUF", "NOT", "AND", "OR", "XOR", "NAND", "NOR"])
        arity = 1 if op in ("BUF", "NOT") else 2
        args = [rng.choice(signals + ["0", "1", "X"]) for _ in range(arity)]
        gates.append({"id": f"g{i}", "op": op, "args": args})
        signals.append(f"g{i}")
    flops = []
    for i in range(rng.randint(0, 2)):
        flops.append({"id": f"q{i}", "d": rng.choice(signals),
                      "initial": rng.choice(["0", "1", "X"])})
        signals.append(f"q{i}")
    outputs = rng.sample(signals, rng.randint(1, min(2, len(signals))))
    fragments = []
    for f in range(rng.randint(1, 4)):
        vectors = []
        for v in range(rng.randint(1, 3)):
            vectors.append({"id": f"f{f}v{v}",
                            "inputs": {name: rng.choice(["0", "1", "X"])
                                       for name in inputs},
                            "expect": {name: rng.choice(["0", "1", "X", "*"])
                                       for name in rng.sample(outputs, rng.randint(0, len(outputs)))}})
        fragments.append({"id": f"frag{f}", "vectors": vectors})
    fault_gates = rng.sample([g["id"] for g in gates],
                             rng.randint(1, min(3, len(gates))))
    return {"inputs": inputs, "gates": gates, "flops": flops, "outputs": outputs,
            "fragments": fragments,
            "faults": [{"gate": gate, "stuck_at": rng.choice(["0", "1"])}
                       for gate in fault_gates],
            "cycle_budget": rng.randint(0, 8),
            "reset_cost": rng.randint(1, 3)}


class CampaignTests(unittest.TestCase):
    def test_continuation_beats_reset_and_piecewise_merge(self):
        # Replaying each fragment from the initial state separately would
        # miss this fault: only prime-then-observe without reset detects it.
        result = run_campaign(prime_observe("1"))
        self.assertEqual(steps(result), [("prime", False), ("observe", False)])
        self.assertEqual(result["total_cost"], 2)
        self.assertEqual(result["undetected_faults"], [])
        self.assertEqual(len(result["detected_faults"]), 1)
        fault = result["detected_faults"][0]
        self.assertEqual((fault["gate"], fault["stuck_at"]), ("g", "0"))
        self.assertEqual(fault["witness"], {"fragment_id": "observe", "vector_id": "o0",
                                            "output": "q", "reference_value": "1",
                                            "faulty_value": "0"})

    def test_reset_erases_fault_state_difference(self):
        # The reset variant of the best schedule stays reference-valid but
        # loses the detection: reset clears the fault-side state difference.
        spec = compile_campaign(prime_observe("1"))
        no_reset = [("prime", False), ("observe", False)]
        with_reset = [("prime", False), ("observe", True)]
        self.assertEqual(len(collect_witnesses(spec, no_reset)), 1)
        self.assertEqual(collect_witnesses(spec, with_reset), {})
        initial = tuple(flop["initial"] for flop in spec["flops"].values())
        for plan in (no_reset, with_reset):
            state, reconciles = initial, True
            for fid, reset in plan:
                if reset:
                    state = initial
                state, observations = simulate_fragment(
                    spec["gates"], spec["flops"], spec["outputs"], spec["order"],
                    spec["fragments"][fid]["vectors"], state, None)
                for vector, obs in zip(spec["fragments"][fid]["vectors"].values(),
                                       observations):
                    reconciles &= all(obs[name] == want
                                      for name, want in vector["expect"].items()
                                      if want != "*")
            self.assertTrue(reconciles)

    def test_reference_invalid_schedule_is_reselected(self):
        # prime with a=0 makes the old best schedule reference-invalid; the
        # witness must not be reused and zero coverage picks the empty plan.
        result = run_campaign(prime_observe("0"))
        self.assertEqual(steps(result), [])
        self.assertEqual(result["total_cost"], 0)
        self.assertEqual(result["detected_faults"], [])
        self.assertEqual(result["undetected_faults"], [{"gate": "g", "stuck_at": "0"}])
        self.assertFalse(result["summary"]["all_faults_detected"])

    def test_multi_fault_budget_tiebreak_and_single_count(self):
        result = run_campaign(multi_fault())
        # Budget 2 cannot fit three 1-cycle fragments or combo+anything, so
        # at most two faults are coverable; [combo] ties [kick,trip] on
        # (detected, cost) and wins the lexicographic sequence tiebreak.
        self.assertEqual(steps(result), [("combo", False)])
        self.assertEqual(result["total_cost"], 2)
        # g1 stuck-at-0 differs on both g1 and g3 in cycle v1 but counts
        # once, with the lexicographically smallest output as witness.
        self.assertEqual(result["summary"]["faults_detected"], 2)
        detected = {row["gate"]: row for row in result["detected_faults"]}
        self.assertEqual(set(detected), {"g1", "g2"})
        self.assertEqual(detected["g1"]["witness"],
                         {"fragment_id": "combo", "vector_id": "v1", "output": "g1",
                          "reference_value": "1", "faulty_value": "0"})
        self.assertEqual(detected["g2"]["witness"],
                         {"fragment_id": "combo", "vector_id": "v2", "output": "g2",
                          "reference_value": "0", "faulty_value": "1"})
        self.assertEqual(result["undetected_faults"], [{"gate": "g3", "stuck_at": "1"}])
        # Detected and undetected lists are sorted by (gate, stuck_at).
        keys = [(row["gate"], row["stuck_at"]) for row in result["detected_faults"]]
        self.assertEqual(keys, sorted(keys))

    def test_unknown_values_are_not_detections(self):
        result = run_campaign(unknown_masking())
        self.assertEqual(steps(result), [("px", False)])
        # h stuck-at-0 is seen in w1; g stuck-at-0 only produces faulty X
        # (w1) or differs under an expect-X row (w2): neither counts.
        self.assertEqual([row["gate"] for row in result["detected_faults"]], ["h"])
        self.assertEqual(result["detected_faults"][0]["witness"],
                         {"fragment_id": "px", "vector_id": "w1", "output": "h",
                          "reference_value": "1", "faulty_value": "0"})
        self.assertEqual(result["undetected_faults"], [{"gate": "g", "stuck_at": "0"}])

    def test_reset_enters_optimal_schedule(self):
        result = run_campaign(reset_needed())
        # load leaves q=1 so probe's q=0 expectation fails without a reset;
        # [probe, reset, load] covers as much but loses the id tiebreak.
        self.assertEqual(steps(result), [("load", False), ("probe", True)])
        self.assertEqual(result["total_cost"], 3)
        self.assertEqual(result["summary"]["resets"], 1)
        detected = {row["gate"]: row for row in result["detected_faults"]}
        self.assertEqual(set(detected), {"g", "h"})
        self.assertEqual(detected["g"]["witness"]["fragment_id"], "load")
        self.assertEqual(detected["h"]["witness"]["fragment_id"], "probe")
        self.assertTrue(result["summary"]["all_faults_detected"])

    def test_optimizer_matches_brute_force(self):
        rng = random.Random(20261120)
        for trial in range(150):
            data = random_campaign(rng)
            spec = compile_campaign(data)
            result = run_campaign(data)
            _, plan, detected, witnesses = brute_force(data)
            self.assertEqual(tuple(tuple(step) for step in steps(result)), plan,
                             f"trial {trial}: {json.dumps(data)}")
            self.assertEqual(result["total_cost"],
                             sum(len(spec["fragments"][fid]["vectors"]) +
                                 (spec["reset_cost"] if reset else 0)
                                 for fid, reset in plan))
            got_detected = {(row["gate"], row["stuck_at"])
                            for row in result["detected_faults"]}
            want_detected = {spec["faults"][index] for index in detected}
            self.assertEqual(got_detected, want_detected, f"trial {trial}")
            self.assertEqual(len(got_detected) + len(result["undetected_faults"]),
                             len(spec["faults"]))
            for row in result["detected_faults"]:
                index = spec["faults"].index((row["gate"], row["stuck_at"]))
                self.assertEqual(row["witness"], witnesses[index], f"trial {trial}")

    def test_zero_budget_yields_empty_schedule(self):
        result = run_campaign(prime_observe("1", budget=0))
        self.assertEqual(steps(result), [])
        self.assertEqual(result["total_cost"], 0)
        self.assertEqual(len(result["undetected_faults"]), 1)

    def test_vector_ids_unique_per_fragment_only(self):
        data = prime_observe("1")
        data["fragments"][1]["vectors"][0]["id"] = "p0"  # same id, other fragment
        result = run_campaign(data)
        self.assertEqual(result["summary"]["faults_detected"], 1)
        data["fragments"][0]["vectors"].append(
            {"id": "p0", "inputs": {"a": "1"}, "expect": {"q": "*"}})
        with self.assertRaisesRegex(ValueError, "duplicate vector ID"):
            run_campaign(data)


class CampaignValidationTests(unittest.TestCase):
    def assert_invalid(self, data, pattern):
        with self.assertRaisesRegex(ValueError, pattern):
            run_campaign(data)

    def test_fault_validation(self):
        data = prime_observe("1")
        data["faults"] = [{"gate": "nope", "stuck_at": "0"}]
        self.assert_invalid(data, "declared gate")
        data = prime_observe("1")
        data["faults"] = [{"gate": "g", "stuck_at": "X"}]
        self.assert_invalid(data, "stuck_at")
        data = prime_observe("1")
        data["faults"] = [{"gate": "g", "stuck_at": 1}]
        self.assert_invalid(data, "stuck_at")
        data = prime_observe("1")
        data["faults"] = [{"gate": "g", "stuck_at": "0"},
                          {"gate": "g", "stuck_at": "0"}]
        self.assert_invalid(data, "duplicate fault")
        data = prime_observe("1")
        data["faults"] = []
        self.assert_invalid(data, "faults")

    def test_fragment_and_budget_bounds(self):
        data = prime_observe("1")
        data["fragments"] = data["fragments"] * 4  # 8 fragments
        self.assert_invalid(data, "fragment")
        data = prime_observe("1")
        data["fragments"][0]["vectors"] = []
        self.assert_invalid(data, "vector")
        data = prime_observe("1")
        data["cycle_budget"] = -1
        self.assert_invalid(data, "cycle_budget")
        data = prime_observe("1")
        data["cycle_budget"] = 1_000_001
        self.assert_invalid(data, "cycle_budget")
        data = prime_observe("1")
        data["reset_cost"] = 0
        self.assert_invalid(data, "reset_cost")
        data = prime_observe("1")
        data["cycle_budget"] = 2.5
        self.assert_invalid(data, "cycle_budget")

    def test_unknown_and_missing_fields(self):
        data = prime_observe("1")
        data["vectors"] = []
        self.assert_invalid(data, "unknown")
        data = prime_observe("1")
        del data["reset_cost"]
        self.assert_invalid(data, "missing")
        data = prime_observe("1")
        data["fragments"][0]["note"] = "extra"
        self.assert_invalid(data, "unknown")

    def test_unselected_fragments_still_validated(self):
        data = prime_observe("1")
        data["fragments"].append({"id": "zz-unused", "vectors": [
            {"id": "u0", "inputs": {"a": "1"}, "expect": {"ghost": "1"}}]})
        self.assert_invalid(data, "declared outputs")

    def test_netlist_errors_still_apply(self):
        data = prime_observe("1")
        data["gates"] = [{"id": "g", "op": "BUF", "args": ["g"]}]
        self.assert_invalid(data, "cycle")


if __name__ == "__main__":
    unittest.main()
