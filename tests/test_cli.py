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
