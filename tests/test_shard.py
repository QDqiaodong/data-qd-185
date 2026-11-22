import copy
import json
import unittest

from logic_replay.service import run as run_source
from logic_replay.shard import (SHARD_SCHEMA, ROLLUP_SCHEMA,
                                _validate_archive, netlist_fingerprint,
                                rollup_documents, run_pack_shard,
                                run_rollup_shards)

CIRCUIT = {
    "inputs": ["a", "b"],
    "gates": [{"id": "g", "op": "BUF", "args": ["q"]},
              {"id": "h", "op": "BUF", "args": ["b"]}],
    "flops": [{"id": "q", "d": "a", "initial": "0"}],
    "outputs": ["g"],
}

VECTORS = [
    ("v0", {"a": "0", "b": "0"}, {}),
    ("v1", {"a": "1", "b": "1"}, {}),
    ("v2", {"a": "1", "b": "0"}, {"g": "0"}),
    ("v3", {"a": "0", "b": "1"}, {}),
]


def pack_input(shard_id, first_cycle, indexes, stimuli, stream_id="s1",
               circuit=None):
    return {
        "stream_id": stream_id,
        "shard_id": shard_id,
        "first_cycle": first_cycle,
        "circuit": circuit or CIRCUIT,
        "vectors": [{"id": VECTORS[i][0], "inputs": stimuli[i],
                     "expect": VECTORS[i][2]} for i in indexes],
    }


def stimuli(overrides=None):
    result = [dict(cell[1]) for cell in VECTORS]
    for index, name, value in overrides or ():
        result[index][name] = value
    return result


def pack_all(stim, ids=("A", "B", "C"), spans=((0, (0, 1)), (2, (2,)), (3, (3,)))):
    archives = {}
    for shard_id, (first, indexes) in zip(ids, spans):
        archives[shard_id] = run_pack_shard(
            pack_input(shard_id, first, list(indexes), stim))
    return archives


def rollup(archives, replacements=None, cycle_count=4, stream_id="s1"):
    """Run the document-level rollup core (no filesystem involved)."""
    base_docs = [_validate_archive(copy.deepcopy(doc)) for doc in archives.values()]
    replacement_docs = {}
    for row in replacements or ():
        replacement_docs[row["shard_id"]] = _validate_archive(
            copy.deepcopy(row["path"]))
    return rollup_documents(stream_id, cycle_count, base_docs, replacement_docs)


def source_report(stim, circuit=None):
    circuit = circuit or CIRCUIT
    data = dict(circuit)
    data["vectors"] = [{"id": VECTORS[i][0], "inputs": stim[i],
                        "expect": VECTORS[i][2]} for i in range(len(stim))]
    return run_source(data)


class PackShardTests(unittest.TestCase):
    def test_archive_identity_and_branch_domain(self):
        archive = run_pack_shard(pack_input("A", 0, [0, 1], stimuli()))
        self.assertEqual(archive["schema"], SHARD_SCHEMA)
        self.assertEqual(archive["schema_version"], 5)
        self.assertEqual(archive["stream_id"], "s1")
        self.assertEqual(archive["shard_id"], "A")
        self.assertEqual(archive["first_cycle"], 0)
        self.assertEqual(archive["cycle_count"], 2)
        self.assertEqual(archive["vector_ids"], ["v0", "v1"])
        self.assertEqual(archive["netlist"]["flop_initial"], {"q": "0"})
        self.assertEqual(archive["netlist"]["fingerprint"],
                         netlist_fingerprint(CIRCUIT["inputs"],
                                             {g["id"]: g for g in CIRCUIT["gates"]},
                                             {f["id"]: f for f in CIRCUIT["flops"]},
                                             CIRCUIT["outputs"]))
        keys = [row["key"] for row in archive["entry_states"]]
        self.assertEqual(keys, ["0", "1", "X"])  # one literal X, not two binary cases
        for row in archive["entry_states"]:
            self.assertEqual(set(row), {"entry", "key", "end_state", "comparisons",
                                        "mismatches", "gates"})

    def test_no_flop_means_single_empty_entry(self):
        circuit = {"inputs": ["a"], "gates": [{"id": "g", "op": "BUF", "args": ["a"]}],
                   "flops": [], "outputs": ["g"]}
        data = {"stream_id": "s1", "shard_id": "S", "first_cycle": 0,
                "circuit": circuit,
                "vectors": [{"id": "v0", "inputs": {"a": "0"}, "expect": {}},
                            {"id": "v1", "inputs": {"a": "1"}, "expect": {}}]}
        archive = run_pack_shard(data)
        self.assertEqual(len(archive["entry_states"]), 1)
        self.assertEqual(archive["entry_states"][0]["entry"], {})
        self.assertEqual(archive["entry_states"][0]["key"], "")
        self.assertEqual(archive["netlist"]["flop_ids"], [])

    def test_three_flops_have_27_independent_branches(self):
        circuit = {
            "inputs": ["d0", "d1", "d2"],
            "gates": [],
            "flops": [{"id": "q0", "d": "d0", "initial": "0"},
                      {"id": "q1", "d": "d1", "initial": "1"},
                      {"id": "q2", "d": "d2", "initial": "X"}],
            "outputs": ["q0", "q1", "q2"],
        }
        data = {"stream_id": "s", "shard_id": "S", "first_cycle": 5,
                "circuit": circuit,
                "vectors": [{"id": "w0",
                             "inputs": {"d0": "1", "d1": "X", "d2": "0"},
                             "expect": {}}]}
        archive = run_pack_shard(data)
        self.assertEqual(len(archive["entry_states"]), 27)
        seen = {row["key"] for row in archive["entry_states"]}
        self.assertEqual(len(seen), 27)
        # every branch ends with the same sampled D values, entry stays literal
        for row in archive["entry_states"]:
            self.assertEqual(row["end_state"], {"q0": "1", "q1": "X", "q2": "0"})
        self.assertEqual(archive["first_cycle"], 5)

    def test_archive_holds_no_raw_stimuli_or_executable_netlist(self):
        archive = run_pack_shard(pack_input("A", 0, [0, 1], stimuli()))
        text = json.dumps(archive)
        # no gate operations/arguments, flop D nets or per-cycle input maps
        for forbidden in ('"op"', '"args"', '"d":', "BUF"):
            self.assertNotIn(forbidden, text)
        # the interface section only carries names and initial values
        self.assertEqual(set(archive["netlist"]),
                         {"fingerprint", "inputs", "gate_ids", "flop_ids",
                          "flop_initial", "outputs"})
        # branches never expose raw stimulus vectors or full state traces
        for row in archive["entry_states"]:
            self.assertNotIn("inputs", row)
            self.assertNotIn("trace", row)

    def test_branch_results_match_direct_simulation_for_every_entry(self):
        archive = run_pack_shard(pack_input("A", 0, [0, 1], stimuli()))
        # a = 0 then 1; g follows the pre-edge q value each cycle
        expected_g = {"0": ["0", "0"], "1": ["1", "0"], "X": ["X", "0"]}
        expected_end = {"0": "1", "1": "1", "X": "1"}  # last D sample is a = 1
        for row in archive["entry_states"]:
            key = row["key"]
            self.assertEqual(row["end_state"], {"q": expected_end[key]})
            gate_g = row["gates"]["g"]
            self.assertEqual(gate_g["values"], expected_g[key])
            self.assertEqual(gate_g["first"], expected_g[key][0])
            self.assertEqual(gate_g["last"], expected_g[key][-1])
            # h = BUF(b), b = 0 then 1, independent of flop entry
            self.assertEqual(row["gates"]["h"]["values"], ["0", "1"])
        # no expectations inside A
        self.assertTrue(all(row["comparisons"] == 0
                            and row["mismatches"] == []
                            for row in archive["entry_states"]))

    def test_explicit_x_expectation_compares_literal(self):
        stim = stimuli()
        data = pack_input("B", 2, [2], stim)
        data["vectors"][0]["expect"] = {"g": "X"}
        archive = run_pack_shard(data)
        by_key = {row["key"]: row for row in archive["entry_states"]}
        self.assertEqual(by_key["0"]["comparisons"], 1)
        self.assertEqual(by_key["0"]["mismatches"][0]["actual"], "0")
        self.assertEqual(by_key["X"]["mismatches"], [])  # X entry -> X output matches
        self.assertEqual(by_key["X"]["comparisons"], 1)

    def test_wildcard_and_missing_expectations_never_compare(self):
        stim = stimuli()
        data = pack_input("B", 2, [2], stim)
        data["vectors"][0]["expect"] = {"g": "*"}
        archive = run_pack_shard(data)
        self.assertTrue(all(row["comparisons"] == 0 for row in archive["entry_states"]))
        data["vectors"][0]["expect"] = {}
        archive = run_pack_shard(data)
        self.assertTrue(all(row["comparisons"] == 0 for row in archive["entry_states"]))

    def test_checksum_detects_tampering(self):
        archive = run_pack_shard(pack_input("A", 0, [0, 1], stimuli()))
        tampered = copy.deepcopy(archive)
        tampered["first_cycle"] = 4
        with self.assertRaisesRegex(ValueError, "checksum"):
            rollup({"A": tampered})

    def test_validation_rejects_bad_pack_inputs(self):
        good = pack_input("A", 0, [0, 1], stimuli())
        for mutate, message in [
            (lambda d: d.update(first_cycle=-1), "first_cycle"),
            (lambda d: d.update(first_cycle=511), "cycles 0"),
            (lambda d: d.update(stream_id=""), "stream_id"),
            (lambda d: d.update(extra=1), "unknown"),
            (lambda d: d["vectors"][0]["inputs"].pop("a"), "missing"),
            (lambda d: d["vectors"][0]["inputs"].update(a="Z"), "logic value"),
            (lambda d: d["vectors"][0].update(extra=1), "unknown"),
        ]:
            bad = copy.deepcopy(good)
            mutate(bad)
            with self.assertRaises((ValueError, KeyError, TypeError)):
                run_pack_shard(bad)

    def test_architecture_limits_enforced(self):
        circuit = copy.deepcopy(CIRCUIT)
        # keep q (gate g still reads it) and add three more flops -> 4 total
        circuit["flops"] = [{"id": "q", "d": "a", "initial": "0"}] + [
            {"id": f"q{i}", "d": "a", "initial": "0"} for i in range(3)]
        with self.assertRaisesRegex(ValueError, "flops"):
            run_pack_shard(pack_input("A", 0, [0, 1], stimuli(), circuit=circuit))
        circuit = copy.deepcopy(CIRCUIT)
        # feed a valid net for every output id
        circuit["outputs"] = [f"o{i}" for i in range(17)]
        circuit["gates"] = [{"id": f"o{i}", "op": "BUF", "args": ["q"]}
                            for i in range(17)]
        with self.assertRaisesRegex(ValueError, "outputs"):
            run_pack_shard(pack_input("A", 0, [0, 1], stimuli(), circuit=circuit))
        bad = pack_input("A", 0, [0], stimuli())
        bad["vectors"] = bad["vectors"] * 33
        with self.assertRaisesRegex(ValueError, "vector"):
            run_pack_shard(bad)


class RollupTests(unittest.TestCase):
    def test_old_summary_identical_to_v1_replay(self):
        stim = stimuli()
        report = rollup(pack_all(stim))
        v1 = source_report(stim)
        old = report["old_summary"]
        self.assertEqual(old["final_state"], v1["final_state"])
        self.assertEqual(old["comparisons"], v1["comparisons"])
        self.assertEqual(old["mismatches"], v1["mismatches"])
        self.assertEqual(old["gate_activity"], v1["gate_activity"])
        self.assertEqual(report["schema"], ROLLUP_SCHEMA)
        self.assertEqual(report["corrections"], [])
        self.assertEqual(report["old_summary"], report["new_summary"])

    def test_acceptance_correction_to_zero_removes_mismatch_without_repacking_b(self):
        stim = stimuli()
        archives = pack_all(stim)
        corrected = pack_all(stimuli([(1, "a", "0")]))
        report = rollup(archives, [{"shard_id": "A", "path": corrected["A"]}])
        v1 = source_report(stimuli([(1, "a", "0")]))
        new = report["new_summary"]
        self.assertEqual(new["mismatches"], v1["mismatches"])
        self.assertEqual(new["final_state"], v1["final_state"])
        self.assertEqual(new["gate_activity"], v1["gate_activity"])
        self.assertEqual(new["mismatch_count"], 0)
        self.assertEqual(len(report["corrections"]), 1)
        event = report["corrections"][0]
        self.assertEqual(event["action"], "remove")
        self.assertEqual((event["cycle"], event["output"]), (2, "g"))
        self.assertEqual(event["record"]["actual"], "1")
        # B and C archives are literally the old ones; their results still line up
        self.assertEqual(new["final_state"], report["old_summary"]["final_state"])

    def test_acceptance_correction_to_x_is_one_update_event(self):
        stim = stimuli()
        archives = pack_all(stim)
        corrected = pack_all(stimuli([(1, "a", "X")]))
        report = rollup(archives, [{"shard_id": "A", "path": corrected["A"]}])
        events = report["corrections"]
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["action"], "update")
        self.assertEqual(event["old_record"]["actual"], "1")
        self.assertEqual(event["new_record"]["actual"], "X")
        self.assertEqual(event["new_record"]["expected"], "0")
        v1 = source_report(stimuli([(1, "a", "X")]))
        self.assertEqual(report["new_summary"]["gate_activity"], v1["gate_activity"])
        self.assertEqual(report["new_summary"]["mismatches"], v1["mismatches"])

    def test_acceptance_b_input_x_only_changes_gate_h_activity(self):
        stim = stimuli()
        archives = pack_all(stim)
        corrected = pack_all(stimuli([(1, "b", "X")]))
        report = rollup(archives, [{"shard_id": "A", "path": corrected["A"]}])
        self.assertEqual(report["corrections"], [])
        new, old = report["new_summary"], report["old_summary"]
        self.assertEqual(new["final_state"], old["final_state"])
        self.assertEqual(new["mismatches"], old["mismatches"])
        self.assertEqual(new["gate_activity"]["g"], old["gate_activity"]["g"])
        h = new["gate_activity"]["h"]
        self.assertEqual(h["values_seen"], ["0", "1", "X"])
        self.assertEqual(h["known_transitions"], 1)  # X blocks both seam and internal pairs
        v1 = source_report(stimuli([(1, "b", "X")]))
        self.assertEqual(new["gate_activity"], v1["gate_activity"])

    def test_reverse_correction_drops_x_from_values_seen(self):
        stim_x = stimuli([(1, "b", "X")])
        archives_x = pack_all(stim_x)
        original_a = pack_all(stimuli())["A"]
        report = rollup(archives_x, [{"shard_id": "A", "path": original_a}])
        self.assertEqual(report["corrections"], [])
        h = report["new_summary"]["gate_activity"]["h"]
        self.assertNotIn("X", h["values_seen"])
        self.assertEqual(h["known_transitions"], 3)

    def test_reverse_correction_re_adds_mismatch(self):
        good = stimuli([(1, "a", "0")])
        archives_good = pack_all(good)
        broken_a = pack_all(stimuli())["A"]
        report = rollup(archives_good, [{"shard_id": "A", "path": broken_a}])
        self.assertEqual(report["old_summary"]["mismatch_count"], 0)
        self.assertEqual(len(report["corrections"]), 1)
        event = report["corrections"][0]
        self.assertEqual(event["action"], "add")
        self.assertEqual((event["cycle"], event["output"]), (2, "g"))
        self.assertEqual(event["record"]["actual"], "1")

    def test_correction_can_reach_into_later_shards_then_converge(self):
        # q <- a; a flips earlier so B's observed output changes, then states
        # reconverge before C; gate activity at the seam still differs.
        stim = stimuli()
        archives = pack_all(stim)
        corrected = pack_all(stimuli([(0, "a", "1")]))
        report = rollup(archives, [{"shard_id": "A", "path": corrected["A"]}])
        v1 = source_report(stimuli([(0, "a", "1")]))
        self.assertEqual(report["new_summary"]["final_state"], v1["final_state"])
        self.assertEqual(report["new_summary"]["gate_activity"], v1["gate_activity"])
        self.assertEqual(report["new_summary"]["mismatches"], v1["mismatches"])

    def test_correction_events_sorted_by_cycle_then_output(self):
        # g1 = NOT(a): every expected record fails; changing a 1 -> X on the
        # first cycle updates both outputs' actual values.
        circuit = {
            "inputs": ["a"],
            "gates": [{"id": "g1", "op": "NOT", "args": ["a"]},
                      {"id": "g2", "op": "NOT", "args": ["a"]}],
            "flops": [{"id": "q", "d": "a", "initial": "0"}],
            "outputs": ["g1", "g2"],
        }
        vectors = [("w0", {"a": "1"}, {"g1": "1", "g2": "1"}),
                   ("w1", {"a": "1"}, {"g1": "1", "g2": "1"})]
        old_data = {"stream_id": "s", "shard_id": "S", "first_cycle": 0,
                    "circuit": circuit,
                    "vectors": [{"id": vid, "inputs": ins, "expect": exp}
                                for vid, ins, exp in vectors]}
        new_data = copy.deepcopy(old_data)
        new_data["vectors"][0]["inputs"] = {"a": "X"}
        old_arc, new_arc = run_pack_shard(old_data), run_pack_shard(new_data)
        report = rollup({"S": old_arc}, [{"shard_id": "S", "path": new_arc}],
                        cycle_count=2, stream_id="s")
        keys = [(event["cycle"], event["output"]) for event in report["corrections"]]
        self.assertEqual(keys, sorted(keys))
        self.assertTrue(all(event["action"] == "update"
                            for event in report["corrections"]))
        first = report["corrections"][0]
        self.assertEqual(first["old_record"]["actual"], "0")
        self.assertEqual(first["new_record"]["actual"], "X")

    def test_nonzero_initial_state_drives_first_shard_branch(self):
        circuit = {"inputs": ["a"],
                   "gates": [{"id": "g", "op": "BUF", "args": ["q"]}],
                   "flops": [{"id": "q", "d": "a", "initial": "1"}],
                   "outputs": ["g"]}
        data = {"stream_id": "s", "shard_id": "S", "first_cycle": 0,
                "circuit": circuit,
                "vectors": [{"id": "w0", "inputs": {"a": "0"}, "expect": {}}]}
        report = rollup({"S": run_pack_shard(data)}, cycle_count=1, stream_id="s")
        self.assertEqual(report["old_summary"]["final_state"], {"q": "0"})
        # the selected branch is the initial-value branch "1", whose gate value is 1
        branch = {row["key"]: row for row in
                  run_pack_shard(data)["entry_states"]}
        self.assertEqual(branch["1"]["gates"]["g"]["values"], ["1"])

    def test_multiple_flops_updated_together_across_seams(self):
        circuit = {
            "inputs": ["d0", "d1"],
            "gates": [],
            "flops": [{"id": "q0", "d": "d0", "initial": "0"},
                      {"id": "q1", "d": "d1", "initial": "0"}],
            "outputs": ["q0", "q1"],
        }
        first = {"stream_id": "s", "shard_id": "P", "first_cycle": 0,
                 "circuit": circuit,
                 "vectors": [{"id": "w0", "inputs": {"d0": "1", "d1": "X"},
                              "expect": {"q0": "0", "q1": "0"}}]}
        second = {"stream_id": "s", "shard_id": "Q", "first_cycle": 1,
                  "circuit": circuit,
                  "vectors": [{"id": "w1", "inputs": {"d0": "0", "d1": "1"},
                               "expect": {}}]}
        report = rollup({"P": run_pack_shard(first),
                         "Q": run_pack_shard(second)}, cycle_count=2,
                        stream_id="s")
        summary = report["old_summary"]
        self.assertEqual(summary["final_state"], {"q0": "0", "q1": "1"})
        self.assertEqual(summary["mismatches"], [])  # pre-edge q values still both 0
        self.assertEqual(summary["comparisons"], 2)


class ManifestValidationTests(unittest.TestCase):
    def setUp(self):
        self.stim = stimuli()
        self.archives = pack_all(self.stim)

    def test_coverage_gap_overlap_and_start(self):
        arcs = dict(self.archives)
        del arcs["B"]
        with self.assertRaisesRegex(ValueError, "gap|overlap"):
            rollup(arcs)
        # build a genuinely valid B-shaped shard whose range overlaps C
        overlap = run_pack_shard(pack_input("B", 3, [2], self.stim))
        with self.assertRaisesRegex(ValueError, "gap|overlap"):
            rollup({"A": self.archives["A"], "B": overlap,
                    "C": self.archives["C"]})
        shifted = run_pack_shard(pack_input("A", 1, [0, 1], self.stim))
        with self.assertRaisesRegex(ValueError, "start at cycle 0"):
            rollup({"A": shifted, "B": self.archives["B"],
                    "C": self.archives["C"]})

    def test_cycle_count_must_match_coverage(self):
        with self.assertRaisesRegex(ValueError, "cycle_count"):
            rollup(self.archives, cycle_count=3)
        with self.assertRaisesRegex(ValueError, "cycle_count"):
            rollup(self.archives, cycle_count=5)

    def test_duplicate_shard_and_vector_ids_rejected(self):
        arcs = dict(self.archives)
        arcs["B2"] = copy.deepcopy(self.archives["B"])
        with self.assertRaisesRegex(ValueError, "duplicate shard"):
            rollup(arcs)
        renamed = copy.deepcopy(self.archives["B"])
        renamed["shard_id"] = "D"
        renamed["summary"]["shard_id"] = "D"
        renamed["vector_ids"] = ["v0"]  # collides with A's vector id
        renamed["checksum"] = _rechecksum(renamed)
        with self.assertRaisesRegex(ValueError, "vector id"):
            rollup({"A": self.archives["A"], "D": renamed, "C": self.archives["C"]})

    def test_mixed_stream_and_netlist_rejected(self):
        other = copy.deepcopy(self.archives["B"])
        other["stream_id"] = "other"
        other["summary"]["stream_id"] = "other"  # keep section internally consistent
        other["checksum"] = _rechecksum(other)
        with self.assertRaisesRegex(ValueError, "stream"):
            rollup({"A": self.archives["A"], "B": other, "C": self.archives["C"]})
        other = copy.deepcopy(self.archives["B"])
        other["netlist"]["fingerprint"] = "a" * 64
        other["checksum"] = _rechecksum(other)
        with self.assertRaisesRegex(ValueError, "netlist"):
            rollup({"A": self.archives["A"], "B": other, "C": self.archives["C"]})

    def test_duplicate_replacement_requires_manifest_loading(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for name, archive in self.archives.items():
                (directory / f"{name}.json").write_text(json.dumps(archive))
            manifest = {
                "stream_id": "s1", "cycle_count": 4,
                "base": [f"{name}.json" for name in self.archives],
                "replace": [{"shard_id": "A", "path": "A.json"},
                            {"shard_id": "A", "path": "A.json"}],
            }
            path = directory / "manifest.json"
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "duplicate replacement"):
                run_rollup_shards(json.loads(path.read_text()), str(path))

    def test_replacement_rules(self):
        good = self.archives["A"]
        with self.assertRaisesRegex(ValueError, "unknown shard"):
            rollup(self.archives, [{"shard_id": "Z", "path": good}])
        wrong_id = copy.deepcopy(good)
        wrong_id["shard_id"] = "Z"
        wrong_id["summary"]["shard_id"] = "Z"
        wrong_id["checksum"] = _rechecksum(wrong_id)
        with self.assertRaisesRegex(ValueError, "same shard id"):
            rollup(self.archives, [{"shard_id": "A", "path": wrong_id}])
        shifted = copy.deepcopy(good)
        shifted["first_cycle"] = 1
        shifted["summary"]["first_cycle"] = 1
        shifted["checksum"] = _rechecksum(shifted)
        with self.assertRaisesRegex(ValueError, "cycle range"):
            rollup(self.archives, [{"shard_id": "A", "path": shifted}])
        re_vectored = copy.deepcopy(good)
        re_vectored["vector_ids"] = ["x0", "x1"]
        re_vectored["checksum"] = _rechecksum(re_vectored)
        with self.assertRaisesRegex(ValueError, "vector ids"):
            rollup(self.archives, [{"shard_id": "A", "path": re_vectored}])

    def test_unknown_manifest_fields_and_bad_types_rejected(self):
        manifest = {"stream_id": "s1", "cycle_count": 4,
                    "base": list(self.archives.values()), "replace": [],
                    "extra": True}
        with self.assertRaisesRegex(ValueError, "unknown"):
            run_rollup_shards(manifest, "/tmp/manifest.json")
        manifest = {"stream_id": "s1", "cycle_count": 0,
                    "base": list(self.archives.values()), "replace": []}
        with self.assertRaises(ValueError):
            run_rollup_shards(manifest, "/tmp/manifest.json")

    def test_archive_internal_consistency_checked(self):
        good = self.archives["A"]
        broken = copy.deepcopy(good)
        broken["entry_states"][0]["gates"]["g"]["first"] = "X"
        broken["checksum"] = _rechecksum(broken)
        with self.assertRaisesRegex(ValueError, "boundary"):
            rollup({"A": broken})
        broken = copy.deepcopy(good)
        broken["entry_states"][0]["comparisons"] = 99
        broken["checksum"] = _rechecksum(broken)
        with self.assertRaisesRegex(ValueError, "comparisons"):
            rollup({"A": broken})
        broken = copy.deepcopy(good)
        del broken["entry_states"][2]
        broken["checksum"] = _rechecksum(broken)
        with self.assertRaisesRegex(ValueError, "entry_states"):
            rollup({"A": broken})
        broken = copy.deepcopy(good)
        broken["summary"]["cycles"] = 9
        broken["checksum"] = _rechecksum(broken)
        with self.assertRaisesRegex(ValueError, "summary inconsistent"):
            rollup({"A": broken})
        broken = copy.deepcopy(good)
        broken["summary"]["extra"] = 1
        broken["checksum"] = _rechecksum(broken)
        with self.assertRaisesRegex(ValueError, "unknown"):
            rollup({"A": broken})

    def test_fingerprint_ignores_declaration_and_key_order(self):
        reordered = copy.deepcopy(CIRCUIT)
        reordered["inputs"] = ["b", "a"]
        reordered["gates"] = [
            {"args": ["b"], "op": "BUF", "id": "h"},
            {"args": ["q"], "op": "BUF", "id": "g"},
        ]
        reordered["flops"] = [{"initial": "0", "id": "q", "d": "a"}]
        reordered["outputs"] = ["g"]
        arc1 = run_pack_shard(pack_input("A", 0, [0, 1], stimuli()))
        arc2 = run_pack_shard(pack_input("A", 0, [0, 1], stimuli(),
                                         circuit=reordered))
        self.assertEqual(arc1["netlist"]["fingerprint"],
                         arc2["netlist"]["fingerprint"])
        # gate argument order is significant
        swapped_args = copy.deepcopy(CIRCUIT)
        swapped_args["gates"][0] = {"id": "g", "op": "BUF", "args": ["b"]}
        arc3 = run_pack_shard(pack_input("A", 0, [0, 1], stimuli(),
                                         circuit=swapped_args))
        self.assertNotEqual(arc1["netlist"]["fingerprint"],
                            arc3["netlist"]["fingerprint"])

    def test_identical_archives_summarize_identically_when_listing_shuffled(self):
        first = rollup(self.archives)
        shuffled = {name: self.archives[name]
                    for name in reversed(list(self.archives))}
        second = rollup(shuffled)
        self.assertEqual(first["old_summary"], second["old_summary"])


def _rechecksum(archive):
    import hashlib
    from logic_replay.shard import _canonical
    body = {key: value for key, value in archive.items() if key != "checksum"}
    return "sha256:" + hashlib.sha256(_canonical(body)).hexdigest()


if __name__ == "__main__":
    unittest.main()
