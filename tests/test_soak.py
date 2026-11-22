"""Tests for the v7 soak-log endurance record analysis."""
import contextlib
import copy
import io
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from logic_replay.cli import main
from logic_replay.gates import evaluate
from logic_replay.netlist import CONSTANTS
from logic_replay.soak import run_soak


XOR_CIRCUIT = {
    "inputs": ["a"],
    "gates": [{"id": "g", "op": "XOR", "args": ["q", "a"]}],
    "flops": [{"id": "q", "d": "g", "initial": "0"}],
    "outputs": ["q"],
}


def holds_abc():
    return [{"id": "A", "ticks": 3, "inputs": {"a": "1"}},
            {"id": "B", "ticks": 3, "inputs": {"a": "0"}},
            {"id": "C", "ticks": 3, "inputs": {"a": "1"}}]


def soak_doc(first, capture=None, trip_after=2):
    return {
        "circuit": copy.deepcopy(XOR_CIRCUIT),
        "holds": holds_abc(),
        "monitors": [{"id": "m1", "output": "q", "first": first,
                      "through": 8, "stride": 2, "expected": "0",
                      "trip_after": trip_after}],
        "capture": capture or {"before": 1, "after": 1},
    }


def expanded_vectors(holds):
    """The same stimulus expanded into ordinary v1 per-cycle vectors."""
    vectors = []
    clock = 0
    for hold in holds:
        for offset in range(hold["ticks"]):
            vectors.append({"id": f"t{clock}", "inputs": dict(hold["inputs"]),
                            "expect": {}})
            clock += 1
    return vectors


class ReferenceSimulator:
    """Plain per-clock replay semantics, used to cross-check the fast path."""

    def __init__(self, circuit):
        self.inputs = circuit["inputs"]
        self.gates = {gate["id"]: gate for gate in circuit["gates"]}
        self.flops = sorted(circuit["flops"], key=lambda item: item["id"])
        self.outputs = circuit["outputs"]
        self.order = self._order()
        self.state = {item["id"]: item["initial"] for item in self.flops}

    def _order(self):
        pending = {name: set(self.gates[name]["args"]) & set(self.gates)
                   for name in self.gates}
        ready = sorted(name for name, parents in pending.items() if not parents)
        order = []
        while ready:
            name = ready.pop(0)
            order.append(name)
            for other, parents in pending.items():
                if name in parents:
                    parents.remove(name)
                    if not parents and other not in order and other not in ready:
                        ready.append(other)
                        ready.sort()
        if len(order) != len(self.gates):
            raise ValueError("combinational cycle")
        return order

    def step(self, assignment):
        values = dict(CONSTANTS)
        values.update(zip(self.inputs, assignment))
        values.update(self.state)
        for name in self.order:
            gate = self.gates[name]
            values[name] = evaluate(gate["op"], [values[arg] for arg in gate["args"]])
        observed = {name: values[name] for name in self.outputs}
        before = dict(self.state)
        self.state = {item["id"]: values[item["d"]] for item in self.flops}
        return before, observed, dict(self.state)


def reference_trace(circuit, holds):
    """Full per-clock (state_before, outputs, state_after, hold metadata)."""
    simulator = ReferenceSimulator(circuit)
    trace = []
    clock = 0
    for hold in holds:
        assignment = tuple(hold["inputs"][name] for name in circuit["inputs"])
        for offset in range(hold["ticks"]):
            before, observed, after = simulator.step(assignment)
            trace.append({"clock": clock, "hold_id": hold["id"],
                          "hold_offset": offset, "inputs": dict(hold["inputs"]),
                          "state_before_edge": before, "outputs": observed,
                          "state_after_edge": after})
            clock += 1
    return trace, simulator.state


def reference_monitor(trace, monitor):
    """Sample accounting computed by literally walking every clock."""
    samples = mismatches = 0
    streak = 0
    trip_clock = streak_start_clock = None
    streak_start_index = None
    sample_index = 0
    for clock in range(monitor["first"], monitor["through"] + 1):
        if (clock - monitor["first"]) % monitor["stride"]:
            continue
        actual = trace[clock]["outputs"][monitor["output"]]
        samples += 1
        if actual == monitor["expected"]:
            streak = 0
            streak_start_index = None
        else:
            mismatches += 1
            if streak == 0:
                streak_start_index = sample_index
            streak += 1
            if trip_clock is None and streak >= monitor["trip_after"]:
                trip_clock = clock
                streak_start_clock = monitor["first"] + streak_start_index * monitor["stride"]
        sample_index += 1
    return {"samples": samples, "mismatches": mismatches,
            "trip_clock": trip_clock, "streak_start_clock": streak_start_clock}


class ScenarioOneTests(unittest.TestCase):
    def test_first_one_trips_at_three_with_segment_spanning_window(self):
        report = run_soak(soak_doc(1))
        self.assertEqual(report["schema_version"], 7)
        self.assertEqual(report["total_clocks"], 9)
        self.assertEqual(report["monitors"], [{
            "id": "m1", "output": "q", "samples": 4, "mismatches": 3,
            "trip_clock": 3, "streak_start_clock": 1}])
        self.assertEqual(report["first_trip_clock"], 3)
        self.assertEqual(report["trigger_monitor_ids"], ["m1"])
        self.assertEqual(report["final_state"], {"q": "0"})
        window = report["waveform_window"]
        self.assertEqual([row["clock"] for row in window], [2, 3, 4])
        self.assertEqual([(row["hold_id"], row["hold_offset"]) for row in window],
                         [("A", 2), ("B", 0), ("B", 1)])

    def test_first_zero_trips_at_six(self):
        report = run_soak(soak_doc(0))
        self.assertEqual(report["monitors"], [{
            "id": "m1", "output": "q", "samples": 5, "mismatches": 3,
            "trip_clock": 6, "streak_start_clock": 4}])
        self.assertEqual(report["first_trip_clock"], 6)
        window = report["waveform_window"]
        self.assertEqual([row["clock"] for row in window], [5, 6, 7])
        self.assertEqual(window[0]["state_before_edge"], {"q": "1"})
        self.assertNotEqual(window[0]["state_before_edge"], {"q": "0"})
        self.assertEqual(window[1]["state_after_edge"], {"q": "0"})

    def test_matches_expanded_v1_replay_slice_and_final_state(self):
        from logic_replay.service import run as run_v1
        for first in (0, 1):
            doc = soak_doc(first)
            report = run_soak(doc)
            trace, final_state = reference_trace(doc["circuit"], doc["holds"])
            self.assertEqual(report["final_state"], final_state)
            expected = reference_monitor(trace, doc["monitors"][0])
            self.assertEqual({key: value for key, value in
                              report["monitors"][0].items()
                              if key != "id" and key != "output"}, expected)
            lo = report["waveform_window"][0]["clock"]
            hi = report["waveform_window"][-1]["clock"]
            for row, reference in zip(report["waveform_window"], trace[lo:hi + 1]):
                self.assertEqual(row, reference)

            # The same stimulus expanded into an ordinary v1 replay input must
            # produce the identical continuous trajectory and final state.
            v1 = {**copy.deepcopy(doc["circuit"]),
                  "vectors": expanded_vectors(doc["holds"])}
            v1_report = run_v1(v1)
            self.assertEqual(v1_report["final_state"], final_state)
            v1_trace = v1_report["trace"]
            self.assertEqual(len(v1_trace),
                             sum(hold["ticks"] for hold in doc["holds"]))
            for clock in range(9):
                row = v1_trace[clock]
                self.assertEqual(row["state_before_edge"], trace[clock]["state_before_edge"])
                self.assertEqual(row["state_after_edge"], trace[clock]["state_after_edge"])
                self.assertEqual(row["outputs_before_edge"], trace[clock]["outputs"])
                self.assertEqual(row["inputs"], trace[clock]["inputs"])
            for clock, reference in ((r["clock"], r) for r in
                                     report["waveform_window"]):
                self.assertEqual(v1_trace[clock]["state_before_edge"],
                                 reference["state_before_edge"])

    def test_sample_based_accounting_on_every_grid(self):
        doc = soak_doc(1)
        trace, _ = reference_trace(doc["circuit"], doc["holds"])
        for first in range(9):
            for stride in range(1, 5):
                for through in range(first, 9):
                    for expected in ("0", "1", "X"):
                        for trip_after in (1, 2, 3, 100):
                            monitor = {"id": "m", "output": "q", "first": first,
                                       "through": through, "stride": stride,
                                       "expected": expected,
                                       "trip_after": trip_after}
                            probe_doc = copy.deepcopy(doc)
                            probe_doc["monitors"] = [monitor]
                            report = run_soak(probe_doc)
                            self.assertEqual(
                                {key: value for key, value in
                                 report["monitors"][0].items()
                                 if key not in ("id", "output")},
                                reference_monitor(trace, monitor))


class WindowAndStreakTests(unittest.TestCase):
    def test_window_clipped_at_record_ends(self):
        doc = soak_doc(1, capture={"before": 64, "after": 64})
        report = run_soak(doc)
        self.assertEqual(report["first_trip_clock"], 3)
        self.assertEqual([row["clock"] for row in report["waveform_window"]],
                         list(range(0, 9)))
        self.assertEqual(report["summary"]["window_rows"], 9)
        first_row = report["waveform_window"][0]
        self.assertEqual(first_row["clock"], 0)
        self.assertEqual(first_row["hold_id"], "A")
        self.assertEqual(first_row["hold_offset"], 0)
        self.assertEqual(first_row["state_before_edge"], {"q": "0"})

        late = soak_doc(0, capture={"before": 64, "after": 64})
        report = run_soak(late)
        self.assertEqual(report["first_trip_clock"], 6)
        self.assertEqual([row["clock"] for row in report["waveform_window"]],
                         list(range(0, 9)))

    def test_no_trip_delivers_no_window(self):
        doc = soak_doc(1)
        doc["monitors"][0]["trip_after"] = 4
        report = run_soak(doc)
        self.assertEqual(report["monitors"][0]["trip_clock"], None)
        self.assertEqual(report["monitors"][0]["streak_start_clock"], None)
        self.assertIsNone(report["first_trip_clock"])
        self.assertIsNone(report["trigger_monitor_ids"])
        self.assertIsNone(report["waveform_window"])
        self.assertEqual(report["summary"]["window_rows"], 0)
        self.assertEqual(report["summary"]["triggered_monitors"], 0)
        # Accounting still covers every sample up to the end of the record.
        self.assertEqual((report["monitors"][0]["samples"],
                          report["monitors"][0]["mismatches"]), (4, 3))

    def test_one_matching_sample_clears_the_losing_streak(self):
        # q sequence on clocks 0..8: 0,1,0,1,1,1,0,1,0. Expect 1 on grid
        # 1,3,5,7: clocks 1/3/5 match, clock 7 is the single mismatch.
        doc = soak_doc(1)
        doc["monitors"][0]["expected"] = "1"
        report = run_soak(doc)
        self.assertEqual(report["monitors"][0]["mismatches"], 1)
        self.assertIsNone(report["monitors"][0]["trip_clock"])
        # Expect 0 with stride 1 from clock 0, trip_after 3: mismatch at 1,
        # match at 2 clears, mismatches 3,4,5 then the third trips at 5,
        # proving the earlier streak did not survive the match at 2.
        doc["monitors"][0] = {"id": "m1", "output": "q", "first": 0,
                              "through": 8, "stride": 1, "expected": "0",
                              "trip_after": 3}
        report = run_soak(doc)
        self.assertEqual(report["monitors"][0]["trip_clock"], 5)
        self.assertEqual(report["monitors"][0]["streak_start_clock"], 3)

    def test_multiple_monitors_trip_same_clock_and_input_order_kept(self):
        doc = soak_doc(1)
        doc["monitors"] = [
            {"id": "zeta", "output": "q", "first": 0, "through": 8,
             "stride": 1, "expected": "0", "trip_after": 4},
            {"id": "alpha", "output": "q", "first": 1, "through": 8,
             "stride": 2, "expected": "0", "trip_after": 1},
        ]
        report = run_soak(doc)
        # Input order is preserved regardless of ID lexicography.
        self.assertEqual([m["id"] for m in report["monitors"]], ["zeta", "alpha"])
        # alpha samples 1,3,5,7: mismatch at 1 -> trip immediately at 1.
        self.assertEqual(report["monitors"][1]["trip_clock"], 1)
        # zeta samples every clock: mismatches 1,3,4,5,7 -> fourth at 6 trips.
        self.assertEqual(report["monitors"][0]["trip_clock"], 6)
        self.assertEqual(report["monitors"][0]["streak_start_clock"], 3)
        self.assertEqual(report["first_trip_clock"], 1)
        self.assertEqual(report["trigger_monitor_ids"], ["alpha"])

        # Same-clock trip: two probes on the same grid both trip at clock 3.
        twin = soak_doc(1)
        twin["monitors"].append({"id": "m2", "output": "q", "first": 1,
                                 "through": 8, "stride": 2, "expected": "0",
                                 "trip_after": 2})
        report = run_soak(twin)
        self.assertEqual(report["first_trip_clock"], 3)
        self.assertEqual(report["trigger_monitor_ids"], ["m1", "m2"])
        self.assertEqual([m["id"] for m in report["monitors"]], ["m1", "m2"])

    def test_explicit_x_matches_only_x(self):
        circuit = {
            "inputs": ["a"],
            "gates": [{"id": "gx", "op": "XOR", "args": ["a", "a"]}],
            "flops": [{"id": "q", "d": "gx", "initial": "X"}],
            "outputs": ["q"],
        }
        # a=X every clock: XOR(X,X)=X, so the flop stays X.
        doc = {"circuit": circuit,
               "holds": [{"id": "H", "ticks": 6, "inputs": {"a": "X"}}],
               "monitors": [
                   {"id": "want-x", "output": "q", "first": 0, "through": 5,
                    "stride": 1, "expected": "X", "trip_after": 1},
                   {"id": "want-0", "output": "q", "first": 0, "through": 5,
                    "stride": 1, "expected": "0", "trip_after": 2},
               ],
               "capture": {"before": 1, "after": 1}}
        report = run_soak(doc)
        self.assertEqual(report["monitors"][0]["mismatches"], 0)
        self.assertIsNone(report["monitors"][0]["trip_clock"])
        self.assertEqual((report["monitors"][1]["samples"],
                          report["monitors"][1]["mismatches"]), (6, 6))
        self.assertEqual(report["monitors"][1]["trip_clock"], 1)
        self.assertEqual(report["first_trip_clock"], 1)
        self.assertEqual(report["final_state"], {"q": "X"})


class CircuitShapeTests(unittest.TestCase):
    def test_no_flop_circuit(self):
        circuit = {"inputs": ["a", "b"],
                   "gates": [{"id": "ab", "op": "AND", "args": ["a", "b"]}],
                   "flops": [],
                   "outputs": ["ab"]}
        doc = {"circuit": circuit,
               "holds": [{"id": "H1", "ticks": 4, "inputs": {"a": "1", "b": "0"}},
                        {"id": "H2", "ticks": 3, "inputs": {"a": "1", "b": "1"}}],
               "monitors": [{"id": "m", "output": "ab", "first": 0,
                             "through": 6, "stride": 1, "expected": "1",
                             "trip_after": 2}],
               "capture": {"before": 1, "after": 1}}
        report = run_soak(doc)
        self.assertEqual(report["final_state"], {})
        # clocks 0..3 read 0 (four mismatches), clocks 4..6 read 1 (matches).
        self.assertEqual(report["monitors"][0]["samples"], 7)
        self.assertEqual(report["monitors"][0]["mismatches"], 4)
        self.assertEqual(report["monitors"][0]["trip_clock"], 1)
        self.assertEqual(report["monitors"][0]["streak_start_clock"], 0)
        # trip at clock 1 freezes clocks 0..2, all still inside the first hold.
        window = report["waveform_window"]
        self.assertEqual([row["clock"] for row in window], [0, 1, 2])
        self.assertEqual([row["outputs"]["ab"] for row in window],
                         ["0", "0", "0"])
        self.assertEqual([row["state_before_edge"] for row in window], [{}, {}, {}])
        self.assertEqual([row["state_after_edge"] for row in window], [{}, {}, {}])
        # The second hold still drives the final accounting of the run.
        self.assertEqual(report["final_state"], {})
        self.assertEqual(report["total_clocks"], 7)

    def test_multiple_flops_update_simultaneously(self):
        circuit = {"inputs": ["unused"],
                   "gates": [],
                   "flops": [{"id": "q1", "d": "q2", "initial": "0"},
                             {"id": "q2", "d": "q1", "initial": "1"}],
                   "outputs": ["q1", "q2"]}
        doc = {"circuit": circuit,
               "holds": [{"id": "H", "ticks": 5,
                          "inputs": {"unused": "0"}}],
               "monitors": [{"id": "m1", "output": "q1", "first": 0,
                             "through": 4, "stride": 1, "expected": "0",
                             "trip_after": 1}],
               "capture": {"before": 0, "after": 0}}
        report = run_soak(doc)
        window = report["waveform_window"]
        self.assertEqual(report["first_trip_clock"], 1)
        self.assertEqual([row["clock"] for row in window], [1])
        self.assertEqual([row["state_before_edge"] for row in window],
                         [{"q1": "1", "q2": "0"}])
        self.assertEqual(window[0]["state_after_edge"], {"q1": "0", "q2": "1"})
        # The full run still clocks all five edges for the final state.
        self.assertEqual(report["total_clocks"], 5)
        self.assertEqual(report["final_state"], {"q1": "1", "q2": "0"})
        self.assertEqual(report["monitors"][0]["mismatches"], 2)

    def test_window_state_is_continuous_run_not_reinitialized(self):
        # The first window row sits inside B but must show the state reached
        # after all of A, not the netlist initial value.
        report = run_soak(soak_doc(0))
        row = next(row for row in report["waveform_window"]
                   if row["clock"] == 5)
        self.assertEqual(row["hold_id"], "B")
        self.assertEqual(row["state_before_edge"], {"q": "1"})
        self.assertNotEqual(row["state_before_edge"], {"q": "0"})


class RandomizedCrossCheckTests(unittest.TestCase):
    def _run_trial(self, rng, circuit, long_holds):
        hold_count = rng.randint(1, 6)
        holds, clock = [], 0
        upper = 400 if long_holds else 12
        for index in range(hold_count):
            holds.append({"id": f"H{index}",
                          "ticks": rng.randint(1, upper),
                          "inputs": {"a": rng.choice(["0", "1", "X"])}})
            clock += holds[-1]["ticks"]
        trace, final_state = reference_trace(circuit, holds)
        monitors = []
        for index in range(rng.randint(1, 4)):
            first = rng.randint(0, clock - 1)
            through = rng.randint(first, clock - 1)
            monitors.append({
                "id": f"p{index}", "output": "q", "first": first,
                "through": through,
                "stride": rng.choice([1, 2, 3, 5, 7, 11, 13]),
                "expected": rng.choice(["0", "1", "X"]),
                "trip_after": rng.randint(1, 9)})
        doc = {"circuit": copy.deepcopy(circuit), "holds": holds,
               "monitors": monitors,
               "capture": {"before": rng.randint(0, 5),
                           "after": rng.randint(0, 5)}}
        report = run_soak(copy.deepcopy(doc))
        self.assertEqual(report["final_state"], final_state)
        self.assertEqual(report["total_clocks"], clock)
        for row, monitor in zip(report["monitors"], monitors):
            self.assertEqual({key: value for key, value in row.items()
                              if key not in ("id", "output")},
                             reference_monitor(trace, monitor))
        earliest = min((m["trip_clock"] for m in report["monitors"]
                        if m["trip_clock"] is not None), default=None)
        self.assertEqual(report["first_trip_clock"], earliest)
        if earliest is None:
            self.assertIsNone(report["waveform_window"])
            return
        expected_ids = [monitor["id"] for monitor, row in
                        zip(monitors, report["monitors"])
                        if row["trip_clock"] == earliest]
        self.assertEqual(report["trigger_monitor_ids"], expected_ids)
        lo = max(0, earliest - doc["capture"]["before"])
        hi = min(clock - 1, earliest + doc["capture"]["after"])
        self.assertEqual([r["clock"] for r in report["waveform_window"]],
                         list(range(lo, hi + 1)))
        for row, reference in zip(report["waveform_window"],
                                  trace[lo:hi + 1]):
            self.assertEqual(row, reference)

    def test_random_small_records_against_per_clock_reference(self):
        import random
        rng = random.Random(20261122)
        circuit = copy.deepcopy(XOR_CIRCUIT)
        for _ in range(40):
            self._run_trial(rng, circuit, long_holds=False)

    def test_random_long_holds_exercise_periodic_fast_paths(self):
        import random
        rng = random.Random(777)
        circuit = copy.deepcopy(XOR_CIRCUIT)
        for _ in range(30):
            self._run_trial(rng, circuit, long_holds=True)

    def test_random_three_flop_circuit(self):
        import random
        rng = random.Random(9001)
        circuit = {"inputs": ["a", "b"],
                   "gates": [{"id": "g1", "op": "XOR", "args": ["q1", "a"]},
                             {"id": "g2", "op": "AND", "args": ["q2", "b"]}],
                   "flops": [{"id": "q1", "d": "g1", "initial": "0"},
                             {"id": "q2", "d": "g2", "initial": "1"},
                             {"id": "q3", "d": "q1", "initial": "X"}],
                   "outputs": ["q1", "q2", "q3"]}
        for _ in range(25):
            hold_count = rng.randint(1, 4)
            holds, clock = [], 0
            for index in range(hold_count):
                holds.append({
                    "id": f"H{index}", "ticks": rng.randint(1, 100),
                    "inputs": {"a": rng.choice(["0", "1", "X"]),
                               "b": rng.choice(["0", "1", "X"])}})
                clock += holds[-1]["ticks"]
            trace, final_state = reference_trace(circuit, holds)
            output = rng.choice(circuit["outputs"])
            monitor = {"id": "p", "output": output,
                       "first": rng.randint(0, clock - 1),
                       "through": rng.randint(0, clock - 1),
                       "stride": rng.choice([1, 2, 3, 7]),
                       "expected": rng.choice(["0", "1", "X"]),
                       "trip_after": rng.randint(1, 6)}
            if monitor["through"] < monitor["first"]:
                monitor["first"], monitor["through"] = (monitor["through"],
                                                         monitor["first"])
            doc = {"circuit": copy.deepcopy(circuit), "holds": holds,
                   "monitors": [monitor],
                   "capture": {"before": rng.randint(0, 5),
                               "after": rng.randint(0, 5)}}
            report = run_soak(copy.deepcopy(doc))
            self.assertEqual(report["final_state"], final_state)
            self.assertEqual({key: value for key, value in
                              report["monitors"][0].items()
                              if key not in ("id", "output")},
                             reference_monitor(trace, monitor))
            earliest = report["first_trip_clock"]
            if earliest is None:
                self.assertIsNone(report["waveform_window"])
                continue
            lo = max(0, earliest - doc["capture"]["before"])
            hi = min(clock - 1, earliest + doc["capture"]["after"])
            for row, reference in zip(report["waveform_window"],
                                      trace[lo:hi + 1]):
                self.assertEqual(row, reference)


class KeyOrderTests(unittest.TestCase):
    def test_reordered_json_keys_keep_report_meaning(self):
        def reorder(value):
            if isinstance(value, dict):
                items = sorted(value.items(), reverse=True)
                return {key: reorder(item) for key, item in items}
            if isinstance(value, list):
                return [reorder(item) for item in value]
            return value

        base = soak_doc(1)
        reordered = json.loads(json.dumps(reorder(base)))
        self.assertEqual(run_soak(reordered), run_soak(base))
        self.assertEqual(
            [m["id"] for m in run_soak(reordered)["monitors"]],
            [m["id"] for m in run_soak(base)["monitors"]])


class ValidationTests(unittest.TestCase):
    def _base(self):
        return copy.deepcopy(soak_doc(1))

    def assert_rejected(self, mutate):
        doc = self._base()
        mutate(doc)
        with self.assertRaises((ValueError, KeyError, TypeError)):
            run_soak(doc)

    def test_unknown_and_missing_fields_rejected(self):
        self.assert_rejected(lambda doc: doc.update(extra=1))
        self.assert_rejected(lambda doc: doc.pop("holds"))
        self.assert_rejected(lambda doc: doc["holds"][0].update(extra=1))
        self.assert_rejected(lambda doc: doc["monitors"][0].pop("stride"))
        self.assert_rejected(lambda doc: doc["capture"].update(extra=1))
        self.assert_rejected(lambda doc: doc["circuit"].update(vectors=[]))

    def test_bad_types_and_ranges_rejected(self):
        self.assert_rejected(lambda doc: doc["holds"][0].__setitem__("ticks", True))
        self.assert_rejected(lambda doc: doc["holds"][0].__setitem__("ticks", 0))
        self.assert_rejected(lambda doc: doc["holds"][0].__setitem__("ticks", 10**12 + 1))
        self.assert_rejected(lambda doc: doc["holds"][0]["inputs"].__setitem__("a", 1))
        self.assert_rejected(lambda doc: doc["monitors"][0].__setitem__("first", -1))
        self.assert_rejected(lambda doc: doc["monitors"][0].__setitem__("stride", 0))
        self.assert_rejected(lambda doc: doc["monitors"][0].__setitem__("through", 9))
        self.assert_rejected(lambda doc: doc["monitors"][0].__setitem__("expected", "*"))
        self.assert_rejected(lambda doc: doc["capture"].__setitem__("before", 65))
        self.assert_rejected(lambda doc: doc["capture"].__setitem__("after", "1"))
        self.assert_rejected(lambda doc: doc["monitors"][0].__setitem__("first", 1.0))
        self.assert_rejected(lambda doc: doc["monitors"][0].__setitem__("trip_after", True))
        self.assert_rejected(lambda doc: doc["holds"][0].__setitem__("ticks", "3"))

    def test_monitor_bounds_and_aligned_grids(self):
        # through == N - 1 is legal; a single-point grid is legal.
        doc = self._base()
        doc["monitors"][0].update(first=8, through=8, stride=4, trip_after=1)
        report = run_soak(doc)
        self.assertEqual(report["monitors"][0]["samples"], 1)
        # A grid whose last stride would pass through still stops at through.
        doc["monitors"][0].update(first=1, through=7, stride=3)
        report = run_soak(doc)
        self.assertEqual(report["monitors"][0]["samples"], 3)  # clocks 1, 4, 7
        # through at N is rejected.
        self.assert_rejected(lambda d: d["monitors"][0].__setitem__("through", 9))

    def test_duplicate_ids_and_bad_references_rejected(self):
        self.assert_rejected(lambda doc: doc["holds"][1].__setitem__("id", "A"))
        self.assert_rejected(lambda doc: doc["monitors"].append(
            dict(doc["monitors"][0])))
        self.assert_rejected(lambda doc: doc["monitors"][0].__setitem__("output", "ghost"))
        self.assert_rejected(lambda doc: doc["holds"][0]["inputs"].pop("a"))
        self.assert_rejected(lambda doc: doc["holds"][0]["inputs"].__setitem__("x", "0"))

    def test_total_ticks_and_flop_limits_rejected(self):
        doc = self._base()
        doc["holds"] = [{"id": "H1", "ticks": 10**12, "inputs": {"a": "0"}},
                        {"id": "H2", "ticks": 1, "inputs": {"a": "0"}}]
        with self.assertRaises(ValueError):
            run_soak(doc)
        doc = self._base()
        doc["circuit"]["flops"] = [
            {"id": f"q{i}", "d": "a", "initial": "0"} for i in range(9)]
        doc["circuit"]["gates"] = []
        doc["circuit"]["outputs"] = ["q0"]
        with self.assertRaisesRegex(ValueError, "8 flops"):
            run_soak(doc)

    def test_illegal_netlist_rejected(self):
        doc = self._base()
        doc["circuit"]["gates"].extend([
            {"id": "x", "op": "BUF", "args": ["y"]},
            {"id": "y", "op": "BUF", "args": ["x"]}])
        with self.assertRaisesRegex(ValueError, "cycle"):
            run_soak(doc)

    def test_empty_collections_rejected(self):
        self.assert_rejected(lambda doc: doc.update(holds=[]))
        self.assert_rejected(lambda doc: doc.update(monitors=[]))
        self.assert_rejected(lambda doc: doc["circuit"].update(outputs=[]))


def invoke(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with mock.patch("sys.argv", ["logic-replay"] + argv), \
            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            main()
            return 0, stdout.getvalue()
        except SystemExit as exc:
            return exc.code, stdout.getvalue()


class SoakCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.source = self.dir / "soak.json"
        self.source.write_text(json.dumps(soak_doc(1)))

    def test_soak_report_written_and_mismatch_still_exits_zero(self):
        output = self.dir / "report.json"
        code, printed = invoke(["--soak-check", str(self.source),
                                "--output", str(output)])
        self.assertEqual(code, 0)
        report = json.loads(output.read_text())
        self.assertEqual(report["schema_version"], 7)
        self.assertEqual(report["first_trip_clock"], 3)
        self.assertEqual(json.loads(printed)["window_rows"], 3)

    def test_mode_is_exclusive_and_rejects_approvals(self):
        output = self.dir / "out.json"
        replay = self.dir / "replay.json"
        replay.write_text(Path("fixtures/input.json").read_text())
        code, _ = invoke(["--soak-check", str(self.source), "--source",
                          str(replay), "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())
        code, _ = invoke(["--soak-check", str(self.source),
                          "--approvals", str(self.source),
                          "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())
        code, _ = invoke(["--output", str(output)])
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())

    def test_existing_target_and_symlink_never_overwritten(self):
        output = self.dir / "report.json"
        output.write_text('{"keep": true}\n')
        code, _ = invoke(["--soak-check", str(self.source),
                          "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.read_text()), {"keep": True})
        real = self.dir / "real.json"
        real.write_text('{"keep": true}\n')
        link = self.dir / "link.json"
        link.symlink_to(real)
        code, _ = invoke(["--soak-check", str(self.source),
                          "--output", str(link)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(real.read_text()), {"keep": True})

    def test_strict_json_failures_leave_no_report(self):
        bad = self.dir / "bad.json"
        bad.write_text(json.dumps(soak_doc(1)).replace('"ticks": 3',
                                                       '"ticks": true', 1))
        output = self.dir / "half.json"
        code, _ = invoke(["--soak-check", str(bad), "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())

        raw = json.dumps(soak_doc(1))
        duplicated = '{"circuit": {},' + raw[1:]
        dup = self.dir / "dup.json"
        dup.write_text(duplicated)
        code, _ = invoke(["--soak-check", str(dup), "--output",
                          str(self.dir / "dup-out.json")])
        self.assertEqual(code, 2)
        self.assertFalse((self.dir / "dup-out.json").exists())

        nan = self.dir / "nan.json"
        nan.write_text(raw.replace('"initial": "0"', '"initial": NaN', 1))
        code, _ = invoke(["--soak-check", str(nan), "--output",
                          str(self.dir / "nan-out.json")])
        self.assertEqual(code, 2)
        self.assertFalse((self.dir / "nan-out.json").exists())

    def test_original_source_mode_unchanged(self):
        replay = self.dir / "replay.json"
        replay.write_text(Path("fixtures/input.json").read_text())
        output = self.dir / "v1.json"
        code, _ = invoke(["--source", str(replay), "--output", str(output)])
        self.assertEqual(code, 0)
        report = json.loads(output.read_text())
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["summary"]["cycles"], 4)


class SoakHandoffPerfTests(unittest.TestCase):
    """The trillion-clock handoff acceptance sample, generated on demand."""

    FIXTURE = Path("fixtures/soak/handoff-1e12.json")
    TOOL = Path("tools/generate_soak_handoff.py")

    def test_generator_creates_sample_and_refuses_existing_target(self):
        if not self.TOOL.exists():
            self.skipTest("generator missing")
        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "handoff-1e12.json"
            completed = subprocess.run(
                [sys.executable, "-I", str(self.TOOL), str(sample)],
                capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            doc = json.loads(sample.read_text())
            self.assertEqual(doc["holds"][0]["ticks"], 800000000001)
            self.assertEqual(doc["monitors"][1]["stride"], 200000000)
            again = subprocess.run(
                [sys.executable, "-I", str(self.TOOL), str(sample)],
                capture_output=True, text=True)
            self.assertEqual(again.returncode, 2)
            self.assertIn("refusing", again.stderr)

    def test_cli_sample_under_30_seconds_and_256_mib(self):
        if not self.FIXTURE.exists():
            self.skipTest("generated fixture missing; "
                          "run tools/generate_soak_handoff.py")
        project_root = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sample = self.FIXTURE
            output = tmp_path / "report.json"

            # Make the in-tree package importable even without installation:
            # isolated mode still processes an explicit site directory, and a
            # .pth file there adds the project's src/ to sys.path.
            site_dir = tmp_path / "site"
            site_dir.mkdir()
            (site_dir / "logic_replay.pth").write_text(
                str(project_root / "src") + "\n")
            runner = (
                "import runpy, site, sys; "
                f"site.addsitedir({str(site_dir)!r}); "
                "sys.argv = ['logic-replay', '--soak-check', "
                f"{str(sample)!r}, '--output', {str(output)!r}]; "
                "runpy.run_module('logic_replay.cli', run_name='__main__')")
            rlimit_as = 256 * 1024 * 1024
            start = time.perf_counter()
            completed = subprocess.run(
                [sys.executable, "-I", "-c", runner],
                env=dict(os.environ), capture_output=True, text=True,
                preexec_fn=lambda: resource.setrlimit(
                    resource.RLIMIT_AS, (rlimit_as, rlimit_as)))
            elapsed = time.perf_counter() - start
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertLess(elapsed, 30)
            report = json.loads(output.read_text())
            self.assertEqual(report["total_clocks"], 1000000000000)
            self.assertEqual(report["monitors"], [
                {"id": "dense", "output": "q", "samples": 1000000000000,
                 "mismatches": 599999999999, "trip_clock": 800000000002,
                 "streak_start_clock": 800000000001},
                {"id": "sparse", "output": "q", "samples": 5000,
                 "mismatches": 5000, "trip_clock": 800000000001,
                 "streak_start_clock": 1},
            ])
            self.assertEqual(report["first_trip_clock"], 800000000001)
            self.assertEqual(report["trigger_monitor_ids"], ["sparse"])
            self.assertEqual(
                [(row["clock"], row["hold_id"], row["hold_offset"],
                  row["state_before_edge"]["q"])
                 for row in report["waveform_window"]],
                [(799999999999, "A", 799999999999, "1"),
                 (800000000000, "A", 800000000000, "0"),
                 (800000000001, "B", 0, "1"),
                 (800000000002, "B", 1, "1")])
            self.assertEqual(report["summary"]["window_rows"], 4)
            self.assertEqual(report["final_state"], {"q": "1"})


if __name__ == "__main__":
    unittest.main()
