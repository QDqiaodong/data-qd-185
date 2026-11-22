import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from logic_replay.cli import main

CAMPAIGN = {
    "inputs": ["a"],
    "gates": [{"id": "g", "op": "BUF", "args": ["a"]}],
    "flops": [{"id": "q", "d": "g", "initial": "1"}],
    "outputs": ["q"],
    "fragments": [
        {"id": "prime", "vectors": [
            {"id": "p0", "inputs": {"a": "1"}, "expect": {"q": "*"}}]},
        {"id": "observe", "vectors": [
            {"id": "o0", "inputs": {"a": "1"}, "expect": {"q": "1"}}]},
    ],
    "faults": [{"gate": "g", "stuck_at": "0"}],
    "cycle_budget": 3,
    "reset_cost": 1,
}


def invoke(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with mock.patch("sys.argv", ["logic-replay"] + argv), \
            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            main()
            return 0, stdout.getvalue()
        except SystemExit as exc:
            return exc.code, stdout.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.campaign = self.dir / "campaign.json"
        self.campaign.write_text(json.dumps(CAMPAIGN))
        self.source = self.dir / "source.json"
        self.source.write_text(Path("fixtures/input.json").read_text())

    def test_campaign_report_written(self):
        output = self.dir / "report.json"
        code, printed = invoke(["--campaign", str(self.campaign), "--output", str(output)])
        self.assertEqual(code, 0)
        report = json.loads(output.read_text())
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["summary"]["faults_detected"], 1)
        self.assertEqual(json.loads(printed)["total_cost"], 2)

    def test_source_mode_unchanged(self):
        output = self.dir / "v1.json"
        code, _ = invoke(["--source", str(self.source), "--output", str(output)])
        self.assertEqual(code, 0)
        report = json.loads(output.read_text())
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["summary"]["cycles"], 4)

    def test_source_and_campaign_are_mutually_exclusive(self):
        output = self.dir / "out.json"
        code, _ = invoke(["--source", str(self.source), "--campaign",
                          str(self.campaign), "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())
        code, _ = invoke(["--output", str(output)])
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())

    def test_existing_target_kept(self):
        output = self.dir / "report.json"
        output.write_text('{"keep": true}\n')
        code, _ = invoke(["--campaign", str(self.campaign), "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.read_text()), {"keep": True})

    def test_invalid_campaign_leaves_no_report(self):
        bad = self.dir / "bad.json"
        data = dict(CAMPAIGN, faults=[{"gate": "ghost", "stuck_at": "0"}])
        bad.write_text(json.dumps(data))
        output = self.dir / "half.json"
        code, _ = invoke(["--campaign", str(bad), "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()


RECOVER_OK = {
    "inputs": ["a"],
    "gates": [{"id": "nq", "op": "OR", "args": ["q", "a"]}],
    "flops": [{"id": "q", "d": "nq", "initial": "0"}],
    "outputs": ["q"],
    "rows": [
        {"id": "t0", "inputs": {}, "observed": {}},
        {"id": "t1", "inputs": {"a": "0"}, "observed": {}},
        {"id": "t2", "inputs": {"a": "0"}, "observed": {}},
        {"id": "t3", "inputs": {"a": "1"}, "observed": {"q": "1"}},
        {"id": "t4", "inputs": {"a": "0"}, "observed": {}},
    ],
    "windows": [{"id": "w2", "from": "t2", "through": "t2"},
                {"id": "w4", "from": "t4", "through": "t4"}],
}


class RecoverCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.capture = self.dir / "capture.json"
        self.capture.write_text(json.dumps(RECOVER_OK))

    def test_recover_report_written(self):
        output = self.dir / "report.json"
        code, printed = invoke(["--recover", str(self.capture), "--output", str(output)])
        self.assertEqual(code, 0)
        report = json.loads(output.read_text())
        self.assertEqual(report["schema_version"], 4)
        self.assertEqual(report["summary"], json.loads(printed))
        self.assertEqual([row["status"] for row in report["windows"]],
                         ["handoff", "handoff"])

    def test_contradiction_exits_zero_with_report(self):
        data = dict(RECOVER_OK, rows=[
            {"id": "a0", "inputs": {"a": "1"}, "observed": {"q": "0"}},
            {"id": "b1", "inputs": {"a": "1"}, "observed": {"q": "0"}},
        ], windows=[{"id": "w", "from": "a0", "through": "b1"}])
        bad = self.dir / "contr.json"
        bad.write_text(json.dumps(data))
        output = self.dir / "contr-report.json"
        code, _ = invoke(["--recover", str(bad), "--output", str(output)])
        self.assertEqual(code, 0)
        report = json.loads(output.read_text())
        self.assertEqual(report["status"], "contradiction")
        self.assertEqual(report["first_contradiction"], "b1")
        self.assertEqual(report["windows"][0]["blocking_reason"],
                         "capture_contradiction")

    def test_recover_is_mutually_exclusive_and_rejects_approvals(self):
        output = self.dir / "out.json"
        code, _ = invoke(["--recover", str(self.capture),
                          "--source", str(self.source_path()), "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())
        code, _ = invoke(["--recover", str(self.capture),
                          "--approvals", str(self.capture), "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())

    def source_path(self):
        path = self.dir / "source.json"
        path.write_text(Path("fixtures/input.json").read_text())
        return path

    def test_existing_target_kept(self):
        output = self.dir / "report.json"
        output.write_text('{"keep": true}\n')
        code, _ = invoke(["--recover", str(self.capture), "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.read_text()), {"keep": True})

    def test_existing_symlink_target_not_followed(self):
        real = self.dir / "real.json"
        real.write_text('{"keep": true}\n')
        output = self.dir / "link.json"
        output.symlink_to(real)
        code, _ = invoke(["--recover", str(self.capture), "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(real.read_text()), {"keep": True})

    def test_invalid_input_leaves_no_report(self):
        data = dict(RECOVER_OK, windows=[{"id": "w", "from": "t0", "through": "ghost"}])
        bad = self.dir / "bad.json"
        bad.write_text(json.dumps(data))
        output = self.dir / "half.json"
        code, _ = invoke(["--recover", str(bad), "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())

    def test_duplicate_json_keys_rejected(self):
        raw = json.dumps(RECOVER_OK)
        # Re-emit with a duplicated top-level "rows" key.
        duplicated = '{"rows": [],' + raw[1:]
        path = self.dir / "dup.json"
        path.write_text(duplicated)
        output = self.dir / "dup-out.json"
        code, _ = invoke(["--recover", str(path), "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())

    def test_nonfinite_constant_rejected(self):
        path = self.dir / "nan.json"
        path.write_text(json.dumps(RECOVER_OK).replace(
            '"initial": "0"', '"initial": NaN', 1))
        output = self.dir / "nan-out.json"
        code, _ = invoke(["--recover", str(path), "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())
