import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from logic_replay.cli import main
from logic_replay.qualify import run_qualify


def sequential_pair():
    """Same interface and behavior; candidate adds a buffer stage and
    declares outputs in the other order. q carries state across vectors."""
    return {
        "reference": {"revision": "rev-old",
                      "netlist": {"inputs": ["a"],
                                  "gates": [{"id": "g", "op": "BUF", "args": ["a"]}],
                                  "flops": [{"id": "q", "d": "g", "initial": "0"}],
                                  "outputs": ["q", "g"]}},
        "candidate": {"revision": "rev-new",
                      "netlist": {"inputs": ["a"],
                                  "gates": [{"id": "g1", "op": "BUF", "args": ["a"]},
                                            {"id": "g", "op": "BUF", "args": ["g1"]}],
                                  "flops": [{"id": "q", "d": "g", "initial": "0"}],
                                  "outputs": ["g", "q"]}},
        "vectors": [
            {"id": "v0", "inputs": {"a": "1"}, "expect": {"q": "0", "g": "1"}},
            {"id": "v1", "inputs": {"a": "0"}, "expect": {"q": "1", "g": "0"}},
            {"id": "v2", "inputs": {"a": "0"}, "expect": {"q": "0", "g": "0"}},
        ],
    }


def deviation_pair():
    """Combinational OR -> XOR redesign: one intentional deviation at v11/g.
    The candidate declares the inputs permuted, which is not a difference."""
    return {
        "reference": {"revision": "rev-old",
                      "netlist": {"inputs": ["a", "b"],
                                  "gates": [{"id": "g", "op": "OR", "args": ["a", "b"]}],
                                  "flops": [],
                                  "outputs": ["g"]}},
        "candidate": {"revision": "rev-new",
                      "netlist": {"inputs": ["b", "a"],
                                  "gates": [{"id": "g", "op": "XOR", "args": ["a", "b"]}],
                                  "flops": [],
                                  "outputs": ["g"]}},
        "vectors": [
            {"id": "v00", "inputs": {"a": "0", "b": "0"}, "expect": {"g": "0"}},
            {"id": "v01", "inputs": {"a": "0", "b": "1"}, "expect": {"g": "1"}},
            {"id": "v11", "inputs": {"a": "1", "b": "1"}, "expect": {"g": "1"}},
        ],
    }


def exact_approval(**overrides):
    approval = {"reference_revision": "rev-old", "candidate_revision": "rev-new",
                "vector_id": "v11", "output": "g",
                "reference_value": "1", "candidate_value": "0",
                "ticket": "COURSE-7", "reason": "OR 改 XOR 是有意的教学变更"}
    approval.update(overrides)
    return approval


def empty_approvals():
    return {"approvals": []}


def shuffle_keys(value):
    """Recursively reverse every JSON object's key order; arrays keep order."""
    if isinstance(value, dict):
        return {key: shuffle_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [shuffle_keys(item) for item in value]
    return value


def invoke(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with mock.patch("sys.argv", ["logic-replay"] + argv), \
            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            main()
            return 0, stdout.getvalue()
        except SystemExit as exc:
            return exc.code, stdout.getvalue()


class QualifyTests(unittest.TestCase):
    def test_state_carries_across_vectors_without_reset(self):
        report = run_qualify(sequential_pair(), empty_approvals())
        self.assertTrue(report["eligible"])
        self.assertEqual(report["reference_contract"], "valid")
        ref_trace = report["reference"]["trace"]
        cand_trace = report["candidate"]["trace"]
        # q loaded in v0 is observed in v1 and cleared after v1: no resets.
        self.assertEqual([row["state_before_edge"]["q"] for row in ref_trace],
                         ["0", "1", "0"])
        self.assertEqual([row["state_after_edge"]["q"] for row in ref_trace],
                         ["1", "0", "0"])
        self.assertEqual([row["state_before_edge"]["q"] for row in cand_trace],
                         ["0", "1", "0"])
        self.assertEqual([row["vector_id"] for row in ref_trace], ["v0", "v1", "v2"])
        # Each version's trace echoes its own outputs declaration order.
        self.assertEqual(list(ref_trace[0]["outputs_before_edge"]), ["q", "g"])
        self.assertEqual(list(cand_trace[0]["outputs_before_edge"]), ["g", "q"])
        self.assertEqual(report["summary"]["comparisons"], 6)
        self.assertEqual({row["status"] for row in report["reconciliation"]}, {"match"})
        self.assertEqual(report["differences"], [])

    def test_reference_invalid_lists_differences_but_not_eligible(self):
        data = sequential_pair()
        data["vectors"][1]["expect"]["q"] = "0"  # reference produces "1" here
        data["candidate"]["netlist"]["gates"] = [{"id": "g", "op": "NOT", "args": ["a"]}]
        report = run_qualify(data, empty_approvals())
        self.assertEqual(report["reference_contract"], "invalid")
        self.assertFalse(report["eligible"])
        # Candidate replay still completed and differences are listed.
        self.assertEqual(len(report["candidate"]["trace"]), 3)
        self.assertEqual([row["vector_id"] for row in report["candidate"]["trace"]],
                         ["v0", "v1", "v2"])
        self.assertGreater(len(report["differences"]), 0)
        self.assertGreater(len(report["unapproved_regressions"]), 0)
        self.assertEqual(report["summary"]["reference_mismatches"], 1)
        self.assertEqual(report["summary"]["comparisons"], 6)

    def test_exact_approval_flips_eligibility(self):
        data = deviation_pair()
        rejected = run_qualify(data, empty_approvals())
        self.assertFalse(rejected["eligible"])
        self.assertEqual(len(rejected["unapproved_regressions"]), 1)
        cell = rejected["unapproved_regressions"][0]
        self.assertEqual((cell["vector_id"], cell["output"]), ("v11", "g"))
        self.assertEqual((cell["reference_value"], cell["candidate_value"]), ("1", "0"))
        approved = run_qualify(data, {"approvals": [exact_approval()]})
        self.assertTrue(approved["eligible"])
        self.assertEqual(approved["unapproved_regressions"], [])
        self.assertEqual(approved["stale_approvals"], [])
        self.assertEqual(len(approved["approved_changes"]), 1)
        change = approved["approved_changes"][0]
        self.assertEqual(change["ticket"], "COURSE-7")
        self.assertEqual(change["reason"], "OR 改 XOR 是有意的教学变更")
        self.assertEqual((change["vector_id"], change["output"]), ("v11", "g"))
        statuses = {row["vector_id"]: row["status"] for row in approved["reconciliation"]}
        self.assertEqual(statuses, {"v00": "match", "v01": "match",
                                    "v11": "approved_change"})

    def test_stale_approvals_block_release(self):
        scenarios = [
            ({"candidate_revision": "rev-other"}, "revision_mismatch"),
            ({"reference_revision": "rev-other"}, "revision_mismatch"),
            ({"candidate_value": "X"}, "value_mismatch"),
            ({"reference_value": "X"}, "value_mismatch"),
            ({"vector_id": "ghost"}, "unknown_vector"),
            ({"output": "ghost"}, "unknown_output"),
            ({"vector_id": "v01", "reference_value": "1",
              "candidate_value": "0"}, "not_a_deviation"),
        ]
        for overrides, detail in scenarios:
            with self.subTest(detail=detail, overrides=overrides):
                report = run_qualify(deviation_pair(),
                                     {"approvals": [exact_approval(**overrides)]})
                self.assertFalse(report["eligible"])
                self.assertEqual(len(report["stale_approvals"]), 1)
                stale = report["stale_approvals"][0]
                self.assertEqual(stale["detail"], detail)
                self.assertEqual(stale["ticket"], "COURSE-7")
                # The real deviation at v11/g stays an unapproved regression.
                self.assertEqual(len(report["unapproved_regressions"]), 1)

    def test_wildcard_missing_and_unknown_expectation_boundaries(self):
        data = {
            "reference": {"revision": "rev-old",
                          "netlist": {"inputs": ["a"],
                                      "gates": [{"id": "g", "op": "BUF", "args": ["a"]}],
                                      "flops": [], "outputs": ["g"]}},
            "candidate": {"revision": "rev-new",
                          "netlist": {"inputs": ["a"],
                                      "gates": [{"id": "g", "op": "XOR",
                                                 "args": ["a", "a"]}],
                                      "flops": [], "outputs": ["g"]}},
            "vectors": [
                {"id": "wild", "inputs": {"a": "1"}, "expect": {"g": "*"}},
                {"id": "known", "inputs": {"a": "0"}, "expect": {"g": "0"}},
                {"id": "missing", "inputs": {"a": "X"}, "expect": {}},
                {"id": "unknown", "inputs": {"a": "X"}, "expect": {"g": "X"}},
            ],
        }
        report = run_qualify(data, empty_approvals())
        self.assertTrue(report["eligible"])
        # Only explicit non-wildcard expectations are reconciled.
        self.assertEqual([row["vector_id"] for row in report["reconciliation"]],
                         ["known", "unknown"])
        # The wildcard cell still shows the raw version difference.
        self.assertEqual([(row["vector_id"], row["output"])
                          for row in report["differences"]], [("wild", "g")])
        # Approvals cannot target wildcard or missing-expectation cells.
        for vector_id in ("wild", "missing"):
            with self.subTest(vector_id=vector_id):
                blocked = run_qualify(data, {"approvals": [exact_approval(
                    vector_id=vector_id, reference_value="1", candidate_value="0")]})
                self.assertFalse(blocked["eligible"])
                self.assertEqual(blocked["stale_approvals"][0]["detail"],
                                 "no_explicit_expectation")

    def test_expect_unknown_is_strict(self):
        data = sequential_pair()
        data["vectors"][0]["expect"]["g"] = "X"  # actual g is "1" in both versions
        report = run_qualify(data, empty_approvals())
        self.assertEqual(report["reference_contract"], "invalid")
        self.assertFalse(report["eligible"])
        # Both versions fail the X expectation identically; the cell is not an
        # authorizable deviation and stays an unapproved regression.
        self.assertEqual(len(report["unapproved_regressions"]), 1)
        cell = report["unapproved_regressions"][0]
        self.assertEqual((cell["vector_id"], cell["output"], cell["expected"]),
                         ("v0", "g", "X"))

    def test_report_structure_and_vector_order(self):
        report = run_qualify(deviation_pair(), {"approvals": [exact_approval()]})
        for key in ("schema_version", "reference_revision", "candidate_revision",
                    "reference_contract", "eligible", "reference", "candidate",
                    "reconciliation", "differences", "approved_changes",
                    "unapproved_regressions", "stale_approvals", "summary"):
            self.assertIn(key, report)
        self.assertEqual(report["schema_version"], 3)
        self.assertEqual((report["reference_revision"], report["candidate_revision"]),
                         ("rev-old", "rev-new"))
        for version in ("reference", "candidate"):
            self.assertEqual([row["vector_id"] for row in report[version]["trace"]],
                             ["v00", "v01", "v11"])
            for row in report[version]["trace"]:
                for field in ("cycle", "vector_id", "state_before_edge",
                              "outputs_before_edge", "state_after_edge"):
                    self.assertIn(field, row)


class QualifyValidationTests(unittest.TestCase):
    def assert_invalid(self, data, approvals=None, pattern=""):
        with self.assertRaisesRegex(ValueError, pattern):
            run_qualify(data, approvals if approvals is not None else empty_approvals())

    def test_revisions_must_differ(self):
        data = deviation_pair()
        data["candidate"]["revision"] = "rev-old"
        self.assert_invalid(data, pattern="revisions must differ")

    def test_interface_sets_must_match(self):
        data = deviation_pair()
        data["candidate"]["netlist"]["inputs"] = ["a", "c"]
        self.assert_invalid(data, pattern="input")
        data = deviation_pair()
        data["candidate"]["netlist"]["outputs"] = ["h"]
        self.assert_invalid(data, pattern="output")

    def test_permuted_interface_is_not_a_difference(self):
        # deviation_pair declares candidate inputs as ["b", "a"]: still fine.
        report = run_qualify(deviation_pair(), {"approvals": [exact_approval()]})
        self.assertTrue(report["eligible"])

    def test_unknown_fields_and_netlist_vectors_rejected(self):
        data = deviation_pair()
        data["note"] = "extra"
        self.assert_invalid(data, pattern="unknown")
        data = deviation_pair()
        data["candidate"]["netlist"]["vectors"] = []
        self.assert_invalid(data, pattern="unknown")
        data = deviation_pair()
        data["candidate"]["note"] = 1
        self.assert_invalid(data, pattern="unknown")

    def test_duplicate_vector_ids_rejected(self):
        data = deviation_pair()
        data["vectors"][1]["id"] = "v00"
        self.assert_invalid(data, pattern="duplicate vector ID")

    def test_empty_vectors_and_blank_revision_rejected(self):
        data = deviation_pair()
        data["vectors"] = []
        self.assert_invalid(data, pattern="vector")
        data = deviation_pair()
        data["reference"]["revision"] = "  "
        self.assert_invalid(data, pattern="revision")

    def test_netlist_rules_still_apply(self):
        data = deviation_pair()
        data["candidate"]["netlist"]["gates"] = [{"id": "g", "op": "BUF", "args": ["g"]}]
        self.assert_invalid(data, pattern="cycle")

    def test_malformed_approvals_rejected(self):
        bad_rows = [
            exact_approval(ticket=42),
            exact_approval(reason="   "),
            exact_approval(reference_value="*"),
            exact_approval(reference_value="1", candidate_value="1"),
            exact_approval(candidate_value=0),
            exact_approval(vector_id=""),
            exact_approval(output="bad name"),
            exact_approval(output="*"),
            {**exact_approval(), "note": "extra"},
            {key: value for key, value in exact_approval().items() if key != "ticket"},
        ]
        for row in bad_rows:
            with self.subTest(row=row):
                self.assert_invalid(deviation_pair(), {"approvals": [row]})
        self.assert_invalid(deviation_pair(), {"approval": []})
        self.assert_invalid(deviation_pair(), {"approvals": {}})

    def test_duplicate_approvals_rejected(self):
        approvals = {"approvals": [exact_approval(), exact_approval()]}
        self.assert_invalid(deviation_pair(), approvals, pattern="duplicate approval")
        # Same cell and version pair is a duplicate even with different values.
        approvals = {"approvals": [exact_approval(),
                                   exact_approval(candidate_value="X")]}
        self.assert_invalid(deviation_pair(), approvals, pattern="duplicate approval")


class KeyOrderTests(unittest.TestCase):
    def test_key_order_does_not_change_report(self):
        approvals = {"approvals": [exact_approval(),
                                   exact_approval(vector_id="ghost", ticket="OLD-1",
                                                  reason="过期票据")]}
        plain = run_qualify(deviation_pair(), approvals)
        shuffled = run_qualify(shuffle_keys(deviation_pair()), shuffle_keys(approvals))
        self.assertEqual(plain, shuffled)
        # The ghost approval is stale and blocks release; v11 is approved.
        self.assertFalse(plain["eligible"])
        self.assertEqual(plain["summary"]["approved_changes"], 1)
        self.assertEqual(plain["summary"]["stale_approvals"], 1)
        self.assertEqual(plain["summary"]["unapproved_regressions"], 0)


class QualifyCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.qualify = self.dir / "qualify.json"
        self.qualify.write_text(json.dumps(deviation_pair()))
        self.approvals = self.dir / "approvals.json"
        self.approvals.write_text(json.dumps({"approvals": [exact_approval()]}))
        self.output = self.dir / "report.json"

    def run_cli(self, approvals_path=None):
        return invoke(["--qualify", str(self.qualify), "--approvals",
                       str(approvals_path or self.approvals),
                       "--output", str(self.output)])

    def test_qualify_report_written(self):
        code, printed = self.run_cli()
        self.assertEqual(code, 0)
        report = json.loads(self.output.read_text())
        self.assertEqual(report["schema_version"], 3)
        self.assertTrue(report["eligible"])
        self.assertTrue(json.loads(printed)["eligible"])

    def test_business_failure_still_writes_report_with_exit_zero(self):
        self.approvals.write_text(json.dumps({"approvals": []}))
        code, _ = self.run_cli()
        self.assertEqual(code, 0)
        report = json.loads(self.output.read_text())
        self.assertFalse(report["eligible"])
        self.assertEqual(len(report["unapproved_regressions"]), 1)

    def test_readme_demo_fixture(self):
        output = self.dir / "demo.json"
        code, _ = invoke(["--qualify", "fixtures/qualify.json", "--approvals",
                          "fixtures/approvals.json", "--output", str(output)])
        self.assertEqual(code, 0)
        report = json.loads(output.read_text())
        self.assertTrue(report["eligible"])
        self.assertEqual(report["summary"]["approved_changes"], 1)
        self.assertEqual(report["approved_changes"][0]["ticket"], "COURSE-142")

    def test_existing_target_and_symlink_keep_exit_two(self):
        self.output.write_text('{"keep": true}\n')
        code, _ = self.run_cli()
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(self.output.read_text()), {"keep": True})
        link = self.dir / "link.json"
        link.symlink_to(self.output)
        code, _ = invoke(["--qualify", str(self.qualify), "--approvals",
                          str(self.approvals), "--output", str(link)])
        self.assertEqual(code, 2)
        self.assertTrue(link.is_symlink())
        self.assertEqual(json.loads(link.read_text()), {"keep": True})

    def test_invalid_input_and_bad_approvals_leave_no_report(self):
        bad = self.dir / "bad-approvals.json"
        bad.write_text(json.dumps({"approvals": [exact_approval(), exact_approval()]}))
        code, _ = self.run_cli(approvals_path=bad)
        self.assertEqual(code, 2)
        self.assertFalse(self.output.exists())
        mismatch = deviation_pair()
        mismatch["candidate"]["netlist"]["outputs"] = ["ghost"]
        self.qualify.write_text(json.dumps(mismatch))
        code, _ = self.run_cli()
        self.assertEqual(code, 2)
        self.assertFalse(self.output.exists())

    def test_mode_and_approvals_flag_combinations(self):
        source = self.dir / "source.json"
        source.write_text(Path("fixtures/input.json").read_text())
        combos = [
            ["--qualify", str(self.qualify), "--output", str(self.output)],
            ["--approvals", str(self.approvals), "--source", str(source),
             "--output", str(self.output)],
            ["--qualify", str(self.qualify), "--approvals", str(self.approvals),
             "--source", str(source), "--output", str(self.output)],
        ]
        for argv in combos:
            with self.subTest(argv=argv):
                code, _ = invoke(argv)
                self.assertEqual(code, 2)
                self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
