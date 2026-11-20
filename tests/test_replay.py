import json
import unittest
from pathlib import Path
from logic_replay.service import run
from logic_replay.gates import evaluate


def fixture():
    return json.loads(Path("fixtures/input.json").read_text())


class LogicTests(unittest.TestCase):
    def test_fixture_clock_observation_and_unknown_final_state(self):
        result = run(fixture())
        self.assertEqual(result["summary"], {"cycles": 4, "gates": 2, "flops": 1,
                                            "comparisons": 4, "mismatches": 0, "all_expected_match": True})
        self.assertEqual([row["outputs_before_edge"]["visible"] for row in result["trace"]],
                         ["0", "1", "1", "0"])
        self.assertEqual(result["final_state"], {"q": "X"})
        self.assertEqual(result["gate_activity"]["visible"]["known_transitions"], 2)

    def test_all_flops_sample_old_state_simultaneously(self):
        data = {"inputs": ["unused"], "gates": [],
                "flops": [{"id": "q1", "d": "q2", "initial": "0"},
                          {"id": "q2", "d": "q1", "initial": "1"}],
                "outputs": ["q1", "q2"],
                "vectors": [{"id": "swap", "inputs": {"unused": "0"}, "expect": {"q1": "0", "q2": "1"}}]}
        result = run(data)
        self.assertEqual(result["final_state"], {"q1": "1", "q2": "0"})

    def test_three_value_controlling_and_unknown_rules(self):
        self.assertEqual(evaluate("AND", ["0", "X"]), "0")
        self.assertEqual(evaluate("OR", ["1", "X"]), "1")
        self.assertEqual(evaluate("NAND", ["1", "X"]), "X")
        self.assertEqual(evaluate("NOR", ["0", "X"]), "X")
        self.assertEqual(evaluate("XOR", ["X", "X"]), "X")

    def test_wildcard_is_not_unknown_expectation(self):
        data = fixture()
        data["vectors"][0]["expect"]["visible"] = "*"
        data["vectors"][1]["expect"]["visible"] = "X"
        result = run(data)
        self.assertEqual(result["comparisons"], 3)
        self.assertEqual(result["mismatches"][0]["vector_id"], "step-1")
        self.assertEqual(result["mismatches"][0]["actual"], "1")
        self.assertFalse(result["summary"]["all_expected_match"])

    def test_combinational_cycles_and_multiple_drivers_reject(self):
        data = fixture()
        data["gates"] = [{"id": "toggle", "op": "BUF", "args": ["visible"]},
                         {"id": "visible", "op": "BUF", "args": ["toggle"]}]
        with self.assertRaisesRegex(ValueError, "cycle"):
            run(data)
        data = fixture()
        data["gates"][0]["id"] = "q"
        with self.assertRaisesRegex(ValueError, "drivers"):
            run(data)

    def test_incomplete_input_vector_rejected(self):
        data = fixture()
        data["vectors"][0]["inputs"] = {}
        with self.assertRaisesRegex(ValueError, "missing"):
            run(data)
