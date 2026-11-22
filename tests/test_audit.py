import contextlib
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

from logic_replay.audit import run_audit
from logic_replay.cli import main

SIGNALS = ["rv", "rr", "rq0", "rc", "sv", "sr", "sq0"]


def invoke(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with mock.patch("sys.argv", ["logic-replay"] + argv), \
            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            main()
            return 0, stdout.getvalue()
        except SystemExit as exc:
            return exc.code, stdout.getvalue()


def source_from_rows(rows, expect=None, signals=None):
    """Rows are plain input-level maps driving BUF gates into same-name outputs."""
    signals = signals or SIGNALS
    gates = [{"id": f"buf_{name}", "op": "BUF", "args": [name]} for name in signals]
    vectors = [
        {"id": f"v{cycle:03d}",
         "inputs": {name: row.get(name, "0") for name in signals},
         "expect": dict(expect or {})}
        for cycle, row in enumerate(rows)
    ]
    return {"inputs": signals, "gates": gates, "flops": [],
            "outputs": list(signals), "vectors": vectors}


def audit_doc(rows, max_age=3, deadlines=None, late_budget=0, expect=None):
    deadlines = deadlines or {"0": 2, "1": 1}
    return {
        "source": source_from_rows(rows, expect),
        "request": {"valid": "rv", "ready": "rr", "tag_bits": ["rq0"], "class_bit": "rc"},
        "response": {"valid": "sv", "ready": "sr", "tag_bits": ["sq0"]},
        "max_age": max_age, "deadlines": deadlines, "late_budget": late_budget,
    }


def handshake_rows(n, requests, responses):
    """requests/responses map cycle -> (tag, class) / tag."""
    rows = [dict(zip(SIGNALS, ["0"] * len(SIGNALS))) for _ in range(n)]
    for cycle, (tag, klass) in requests.items():
        rows[cycle].update(rv="1", rr="1", rq0=tag, rc=klass)
    for cycle, tag in responses.items():
        rows[cycle].update(sv="1", sr="1", sq0=tag)
    return rows


def build(requests, responses, n, max_age=3, deadlines=None, late_budget=0):
    deadlines = deadlines or {"0": 2, "1": 1}
    return run_audit(audit_doc(handshake_rows(n, requests, responses),
                               max_age, deadlines, late_budget))


def reorder_keys(value):
    """Return the same JSON value with every object's keys emitted reversed."""
    if isinstance(value, dict):
        return {key: reorder_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [reorder_keys(item) for item in value]
    return value


class AcceptanceScenarioTests(unittest.TestCase):
    def test_first_acceptance_over_budget_then_review_then_confirmed(self):
        # Requests at 0 (class 0) and 1 (class 1), responses at 2 and 3.
        requests = {0: ("0", "0"), 1: ("0", "1")}
        responses = {2: "0", 3: "0"}

        report = build(requests, responses, 4, deadlines={"0": 2, "1": 1}, late_budget=0)
        self.assertEqual(report["status"], "pairable")
        pairing = report["pairing"]
        self.assertEqual(pairing["confirmed_pairs"], [])
        late_range = pairing["late_range"]
        self.assertEqual((late_range["minimum"], late_range["maximum"]), (1, 1))
        self.assertEqual(report["compliance"]["status"], "over_budget")
        # Every request keeps both responses as candidates, ascending.
        for row in pairing["candidates"]:
            self.assertEqual([c["cycle"] for c in row["candidates"]], [2, 3])
        # Both witnesses are real perfect pairings with exactly one late.
        for witness in (late_range["minimum_witness"], late_range["maximum_witness"]):
            self.assertEqual(len(witness), 2)
            used_requests = {p["request_cycle"] for p in witness}
            used_responses = {p["response_cycle"] for p in witness}
            self.assertEqual(used_requests, {0, 1})
            self.assertEqual(used_responses, {2, 3})
            late = sum(1 for p in witness
                       if p["response_cycle"] - p["request_cycle"]
                       > (2 if p["request_cycle"] == 0 else 1))
            self.assertEqual(late, 1)

        # Class-1 deadline relaxed 1 -> 2: candidates unchanged, range [0, 1].
        report = build(requests, responses, 4, deadlines={"0": 2, "1": 2}, late_budget=0)
        late_range = report["pairing"]["late_range"]
        self.assertEqual((late_range["minimum"], late_range["maximum"]), (0, 1))
        self.assertEqual(report["compliance"]["status"], "needs_review")
        for row in report["pairing"]["candidates"]:
            self.assertEqual([c["cycle"] for c in row["candidates"]], [2, 3])

        # max_age 3 -> 2: both identities become certain, range [0, 0], within.
        report = build(requests, responses, 4, max_age=2,
                       deadlines={"0": 2, "1": 2}, late_budget=0)
        pairing = report["pairing"]
        self.assertEqual(
            [(p["request_cycle"], p["response_cycle"]) for p in pairing["confirmed_pairs"]],
            [(0, 2), (1, 3)])
        self.assertEqual(
            (pairing["late_range"]["minimum"], pairing["late_range"]["maximum"]), (0, 0))
        self.assertEqual(report["compliance"]["status"], "within_budget")

    def test_locally_compatible_request_can_only_publish_forced_pair(self):
        # Requests 0,2 and responses 3,5, same tag, max_age 3.
        report = build({0: ("0", "0"), 2: ("0", "0")}, {3: "0", 5: "0"}, 6,
                       max_age=3, deadlines={"0": 3, "1": 3})
        self.assertEqual(report["status"], "pairable")
        confirmed = [(p["request_cycle"], p["response_cycle"])
                     for p in report["pairing"]["confirmed_pairs"]]
        self.assertEqual(confirmed, [(0, 3), (2, 5)])
        # Request 2 is locally compatible with response 3 but cannot claim it.
        request_two = next(row for row in report["pairing"]["candidates"]
                           if row["request_cycle"] == 2)
        self.assertEqual([c["cycle"] for c in request_two["candidates"]], [5])

    def test_equal_counts_with_local_candidates_can_still_be_unpairable(self):
        # Requests 0,1,3 versus responses 2,4,5, max_age 2: Hall says max 2.
        report = build({0: ("0", "0"), 1: ("0", "0"), 3: ("0", "0")},
                       {2: "0", 4: "0", 5: "0"}, 6, max_age=2,
                       deadlines={"0": 2, "1": 2})
        self.assertEqual(report["status"], "unpairable")
        self.assertEqual(report["pairing"]["maximum_matching"], 2)
        self.assertEqual(len(report["request_events"]), 3)
        self.assertEqual(len(report["response_events"]), 3)
        # No pairing or quality verdict is published.
        self.assertNotIn("confirmed_pairs", report["pairing"])
        self.assertNotIn("late_range", report["pairing"])
        self.assertNotIn("compliance", report)

    def test_empty_batch_has_empty_pairing(self):
        report = build({}, {}, 3, deadlines={"0": 2, "1": 1})
        self.assertEqual(report["status"], "pairable")
        self.assertEqual(report["request_events"], [])
        self.assertEqual(report["response_events"], [])
        self.assertEqual(report["pairing"]["candidates"], [])
        self.assertEqual(report["pairing"]["confirmed_pairs"], [])
        self.assertEqual(report["pairing"]["tardiness"], [])
        self.assertEqual(
            (report["pairing"]["late_range"]["minimum"],
             report["pairing"]["late_range"]["maximum"]), (0, 0))
        self.assertEqual(report["compliance"]["status"], "within_budget")

    def test_different_tags_force_identity(self):
        report = build({0: ("0", "0"), 1: ("1", "0")},
                       {2: "1", 3: "0"}, 4, deadlines={"0": 3, "1": 3})
        confirmed = [(p["request_cycle"], p["response_cycle"])
                     for p in report["pairing"]["confirmed_pairs"]]
        self.assertEqual(confirmed, [(0, 3), (1, 2)])

    def test_same_cycle_two_channels_never_pairs_zero_latency(self):
        report = build({0: ("0", "0")}, {0: "0"}, 2,
                       max_age=3, deadlines={"0": 3, "1": 3})
        self.assertEqual(report["status"], "unpairable")
        self.assertEqual(report["pairing"]["maximum_matching"], 0)
        # Two distinct events on two distinct channels in one cycle.
        self.assertEqual(len(report["request_events"]), 1)
        self.assertEqual(len(report["response_events"]), 1)

    def test_deadline_equality_is_timely(self):
        report = build({0: ("0", "0")}, {2: "0"}, 3,
                       max_age=3, deadlines={"0": 2, "1": 2}, late_budget=0)
        self.assertEqual(report["pairing"]["tardiness"][0]["verdict"], "always_timely")
        self.assertEqual(
            (report["pairing"]["late_range"]["minimum"],
             report["pairing"]["late_range"]["maximum"]), (0, 0))
        self.assertEqual(report["compliance"]["status"], "within_budget")

    def test_latency_past_deadline_still_legal_identity(self):
        # Latency 3 exceeds deadline 2 but stays within max_age 3.
        report = build({0: ("0", "0")}, {3: "0"}, 4,
                       max_age=3, deadlines={"0": 2, "1": 2}, late_budget=0)
        self.assertEqual(report["status"], "pairable")
        self.assertEqual(report["pairing"]["tardiness"][0]["verdict"], "always_late")
        self.assertEqual(report["compliance"]["status"], "over_budget")

    def test_count_mismatch_is_unpairable_with_matching_size(self):
        report = build({0: ("0", "0"), 1: ("0", "0")}, {2: "0"}, 3,
                       max_age=3, deadlines={"0": 3, "1": 3})
        self.assertEqual(report["status"], "unpairable")
        self.assertEqual(report["pairing"]["maximum_matching"], 1)

    def test_two_channels_in_one_cycle_are_two_events(self):
        rows = [dict(zip(SIGNALS, ["0"] * len(SIGNALS)))]
        rows[0].update(rv="1", rr="1", rq0="0", rc="0",
                       sv="1", sr="1", sq0="1")
        report = run_audit(audit_doc(rows, max_age=3,
                                     deadlines={"0": 3, "1": 3}))
        # Different tags and zero latency: nothing pairs, but both events
        # are kept on their respective channel.
        self.assertEqual(report["status"], "unpairable")
        self.assertEqual(report["request_events"][0]["cycle"], 0)
        self.assertEqual(report["response_events"][0]["cycle"], 0)
        self.assertEqual(report["request_events"][0]["tag"], "0")
        self.assertEqual(report["response_events"][0]["tag"], "1")

    def test_needs_review_when_budget_between_extrema(self):
        # First acceptance shape gives range [1, 1] with budget 0 (over); the
        # relaxed deadlines give [0, 1], so budget 0 lands between the extrema.
        report = build({0: ("0", "0"), 1: ("0", "1")}, {2: "0", 3: "0"}, 4,
                       deadlines={"0": 2, "1": 2}, late_budget=0)
        self.assertEqual(report["compliance"]["status"], "needs_review")
        # The same range with budget 1 is provably within the budget.
        report = build({0: ("0", "0"), 1: ("0", "1")}, {2: "0", 3: "0"}, 4,
                       deadlines={"0": 2, "1": 2}, late_budget=1)
        self.assertEqual(report["compliance"]["status"], "within_budget")

    def test_per_request_tardiness_either_when_candidates_differ(self):
        report = build({0: ("0", "0"), 1: ("0", "1")}, {2: "0", 3: "0"}, 4,
                       deadlines={"0": 2, "1": 1}, late_budget=0)
        verdicts = {row["request_cycle"]: row["verdict"]
                    for row in report["pairing"]["tardiness"]}
        # Request 0: latency 2 timely, 3 late; request 1: latency 1 timely,
        # 2 late against deadline 1. Both see both outcomes.
        self.assertEqual(verdicts, {0: "either", 1: "either"})


class ObservationTests(unittest.TestCase):
    def flop_doc(self, rows):
        # Flip-flop q toggles on a=1; rrv = BUF(q) drives a handshake bit,
        # so pre-edge and post-edge sampling give different values in cycle 0.
        gates = [
            {"id": "d_g", "op": "XOR", "args": ["a", "q"]},
            {"id": "rrv", "op": "BUF", "args": ["q"]},
            {"id": "rr", "op": "BUF", "args": ["rin"]},
            {"id": "rq0", "op": "BUF", "args": ["tin"]},
            {"id": "rc", "op": "BUF", "args": ["cin"]},
            {"id": "sv", "op": "BUF", "args": ["sin1"]},
            {"id": "sr", "op": "BUF", "args": ["sin2"]},
            {"id": "sq0", "op": "BUF", "args": ["sin3"]},
        ]
        flops = [{"id": "q", "d": "d_g", "initial": "0"}]
        vectors = [{"id": f"v{cycle}", "inputs": row, "expect": {}}
                   for cycle, row in enumerate(rows)]
        source = {"inputs": ["a", "rin", "tin", "cin", "sin1", "sin2", "sin3"],
                  "gates": gates, "flops": flops,
                  "outputs": ["rrv", "rr", "rq0", "rc", "sv", "sr", "sq0"],
                  "vectors": vectors}
        return {
            "source": source,
            "request": {"valid": "rrv", "ready": "rr", "tag_bits": ["rq0"],
                        "class_bit": "rc"},
            "response": {"valid": "sv", "ready": "sr", "tag_bits": ["sq0"]},
            "max_age": 3, "deadlines": {"0": 2, "1": 1}, "late_budget": 0,
        }

    def test_handshake_bit_sampled_before_edge(self):
        rows = [
            {"a": "1", "rin": "1", "tin": "0", "cin": "0",
             "sin1": "0", "sin2": "0", "sin3": "0"},
            {"a": "1", "rin": "1", "tin": "0", "cin": "0",
             "sin1": "0", "sin2": "0", "sin3": "0"},
        ]
        report = run_audit(self.flop_doc(rows))
        # Pre-edge q is 0 in cycle 0, 1 in cycle 1: exactly one request event.
        self.assertEqual([event["cycle"] for event in report["request_events"]], [1])

    def test_x_suppressed_by_zero_handshake_bit(self):
        rows = [{"rv": "X", "rr": "0", "rq0": "X", "rc": "X",
                 "sv": "0", "sr": "0", "sq0": "0"}]
        report = run_audit(audit_doc(rows))
        self.assertEqual(report["status"], "pairable")
        self.assertEqual(report["request_events"], [])

    def test_unsuppressed_handshake_ambiguity_marks_batch_unobservable(self):
        rows = [{"rv": "X", "rr": "1", "rq0": "0", "rc": "0",
                 "sv": "0", "sr": "0", "sq0": "0"}]
        report = run_audit(audit_doc(rows))
        self.assertEqual(report["status"], "unobservable")
        self.assertEqual(report["observation_issues"], [
            {"cycle": 0, "vector_id": "v000", "channel": "request",
             "reason": "handshake_unknown"}])
        self.assertNotIn("pairing", report)
        self.assertNotIn("compliance", report)

    def test_confirmed_handshake_with_unknown_tag_is_unobservable(self):
        rows = [{"rv": "1", "rr": "1", "rq0": "X", "rc": "0",
                 "sv": "0", "sr": "0", "sq0": "0"}]
        report = run_audit(audit_doc(rows))
        self.assertEqual(report["status"], "unobservable")
        self.assertEqual(report["observation_issues"][0]["reason"], "tag_unknown")

    def test_confirmed_handshake_with_unknown_class_is_unobservable(self):
        rows = [{"rv": "1", "rr": "1", "rq0": "0", "rc": "X",
                 "sv": "0", "sr": "0", "sq0": "0"}]
        report = run_audit(audit_doc(rows))
        self.assertEqual(report["status"], "unobservable")
        self.assertEqual(report["observation_issues"][0]["reason"], "class_unknown")

    def test_issues_listed_in_cycle_then_channel_order(self):
        rows = [
            {"rv": "X", "rr": "1", "rq0": "0", "rc": "0",
             "sv": "1", "sr": "X", "sq0": "0"},
            {"rv": "1", "rr": "1", "rq0": "X", "rc": "0",
             "sv": "0", "sr": "0", "sq0": "0"},
        ]
        report = run_audit(audit_doc(rows))
        self.assertEqual(report["status"], "unobservable")
        self.assertEqual(
            [(i["cycle"], i["channel"]) for i in report["observation_issues"]],
            [(0, "request"), (0, "response"), (1, "request")])

    def test_expectation_mismatch_unrelated_to_audit_does_not_change_result(self):
        rows = [{"rv": "0", "rr": "0", "rq0": "0", "rc": "0",
                 "sv": "1", "sr": "1", "sq0": "0"}]
        clean = run_audit(audit_doc(rows))
        mismatched = run_audit(audit_doc(rows, expect={"rv": "1"}))
        self.assertEqual(clean["status"], mismatched["status"])
        self.assertEqual(clean["request_events"], mismatched["request_events"])
        self.assertEqual(clean["response_events"], mismatched["response_events"])
        self.assertEqual(clean["pairing"], mismatched["pairing"])

    def test_multi_bit_tag_read_high_first_and_keeps_leading_zero(self):
        signals = ["rv", "rr", "th", "tl", "rc", "sv", "sr", "sh", "sl"]
        rows = [
            dict(zip(signals, ["0"] * len(signals))),
            dict(zip(signals, ["0"] * len(signals))),
        ]
        rows[0].update(rv="1", rr="1", th="0", tl="1", rc="0")
        rows[1].update(sv="1", sr="1", sh="0", sl="1")
        gates = [{"id": f"buf_{name}", "op": "BUF", "args": [name]} for name in signals]
        source = {"inputs": signals, "gates": gates, "flops": [],
                  "outputs": list(signals),
                  "vectors": [
                      {"id": "v0", "inputs": rows[0], "expect": {}},
                      {"id": "v1", "inputs": rows[1], "expect": {}},
                  ]}
        doc = {
            "source": source,
            "request": {"valid": "rv", "ready": "rr", "tag_bits": ["th", "tl"],
                        "class_bit": "rc"},
            "response": {"valid": "sv", "ready": "sr", "tag_bits": ["sh", "sl"]},
            "max_age": 3, "deadlines": {"0": 2, "1": 1}, "late_budget": 0,
        }
        report = run_audit(doc)
        self.assertEqual(report["parameters"]["tag_width"], 2)
        self.assertEqual(report["request_events"][0]["tag"], "01")
        self.assertEqual(report["response_events"][0]["tag"], "01")


class DeterminismTests(unittest.TestCase):
    def test_reordered_json_keys_leave_report_unchanged(self):
        requests = {0: ("0", "0"), 1: ("0", "1"), 2: ("1", "0")}
        responses = {3: "0", 4: "0", 5: "1"}
        doc = audit_doc(handshake_rows(6, requests, responses),
                        max_age=5, deadlines={"0": 2, "1": 1}, late_budget=1)
        first = run_audit(doc)
        second = run_audit(reorder_keys(doc))
        self.assertEqual(first, second)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.rows = handshake_rows(
            2, {0: ("0", "0")}, {1: "0"})
        self.doc = audit_doc(self.rows, deadlines={"0": 2, "1": 1})

    def write(self, name, payload, raw=None):
        path = self.dir / name
        path.write_text(raw if raw is not None else json.dumps(payload))
        return path

    def assert_rejected(self, payload=None, raw=None):
        path = self.write("in.json", payload or self.doc, raw)
        output = self.dir / "out.json"
        code, _ = invoke(["--audit-transfers", str(path), "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())

    def test_unknown_top_level_field_rejected(self):
        self.assert_rejected({**self.doc, "extra": 1})

    def test_missing_field_rejected(self):
        broken = dict(self.doc)
        del broken["late_budget"]
        self.assert_rejected(broken)

    def test_unknown_signal_reference_rejected(self):
        broken = json.loads(json.dumps(self.doc))
        broken["request"]["valid"] = "ghost"
        self.assert_rejected(broken)

    def test_duplicate_tag_bit_reference_rejected(self):
        broken = json.loads(json.dumps(self.doc))
        broken["request"]["tag_bits"] = ["rq0", "rq0"]
        self.assert_rejected(broken)

    def test_tag_width_mismatch_rejected(self):
        broken = json.loads(json.dumps(self.doc))
        broken["response"]["tag_bits"] = ["sq0", "rr"]
        self.assert_rejected(broken)

    def test_bad_source_netlist_rejected(self):
        broken = json.loads(json.dumps(self.doc))
        broken["source"]["gates"] = [
            {"id": "loop1", "op": "BUF", "args": ["loop2"]},
            {"id": "loop2", "op": "BUF", "args": ["loop1"]}]
        self.assert_rejected(broken)

    def test_ranges_enforced(self):
        for field, value in (("max_age", 0), ("max_age", 1000),
                             ("late_budget", -1), ("late_budget", 1001)):
            broken = json.loads(json.dumps(self.doc))
            broken[field] = value
            self.assert_rejected(broken)
        broken = json.loads(json.dumps(self.doc))
        broken["deadlines"] = {"0": 999, "1": 1}
        self.assert_rejected(broken)
        broken = json.loads(json.dumps(self.doc))
        broken["deadlines"] = {"0": 1}
        self.assert_rejected(broken)
        broken = json.loads(json.dumps(self.doc))
        broken["deadlines"] = {"0": 1, "1": 1, "2": 1}
        self.assert_rejected(broken)

    def test_wrong_types_rejected(self):
        broken = json.loads(json.dumps(self.doc))
        broken["max_age"] = "3"
        self.assert_rejected(broken)
        broken = json.loads(json.dumps(self.doc))
        broken["request"]["tag_bits"] = "rq0"
        self.assert_rejected(broken)
        broken = json.loads(json.dumps(self.doc))
        broken["source"] = []
        self.assert_rejected(broken)

    def test_duplicate_json_keys_rejected(self):
        raw = json.dumps(self.doc)
        duplicated = '{"late_budget": 9,' + raw[1:]
        self.assert_rejected(raw=duplicated)

    def test_nonfinite_constant_rejected(self):
        raw = json.dumps(self.doc).replace('"max_age": 3', '"max_age": NaN', 1)
        self.assert_rejected(raw=raw)

    def test_mutual_exclusion_and_approvals_rejected(self):
        path = self.write("in.json", self.doc)
        other = self.write("src.json", json.loads(Path("fixtures/input.json").read_text()))
        output = self.dir / "out.json"
        code, _ = invoke(["--audit-transfers", str(path), "--source", str(other),
                          "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())
        code, _ = invoke(["--audit-transfers", str(path),
                          "--approvals", str(path), "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())

    def test_existing_target_and_symlink_never_overwritten(self):
        path = self.write("in.json", self.doc)
        output = self.dir / "report.json"
        output.write_text('{"keep": true}\n')
        code, _ = invoke(["--audit-transfers", str(path), "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.read_text()), {"keep": True})
        real = self.dir / "real.json"
        real.write_text('{"keep": true}\n')
        link = self.dir / "link.json"
        link.symlink_to(real)
        code, _ = invoke(["--audit-transfers", str(path), "--output", str(link)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(real.read_text()), {"keep": True})


class CliReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_business_outcomes_exit_zero_with_full_report(self):
        scenarios = {
            "unobservable": [{"rv": "X", "rr": "1", "rq0": "0", "rc": "0",
                              "sv": "0", "sr": "0", "sq0": "0"}],
            "unpairable": handshake_rows(2, {0: ("0", "0")}, {1: "0"}),
            "over": handshake_rows(4, {0: ("0", "0"), 1: ("0", "1")},
                                   {2: "0", 3: "0"}),
        }
        for name, rows in scenarios.items():
            with self.subTest(name):
                source = self.dir / f"{name}.json"
                source.write_text(json.dumps(audit_doc(rows)))
                output = self.dir / f"{name}-out.json"
                code, printed = invoke(
                    ["--audit-transfers", str(source), "--output", str(output)])
                self.assertEqual(code, 0)
                report = json.loads(output.read_text())
                self.assertEqual(report["schema_version"], 6)
                self.assertEqual(json.loads(printed), report["summary"])


class HandoffSampleTests(unittest.TestCase):
    """The minute-handoff acceptance sample: 256 requests then 256 responses."""

    @staticmethod
    def sample_doc():
        rows = []
        for cycle in range(512):
            row = dict(zip(SIGNALS, ["0"] * len(SIGNALS)))
            if cycle < 256:
                row.update(rv="1", rr="1", rq0="0", rc="0")
            else:
                row.update(sv="1", sr="1", sq0="0")
            rows.append(row)
        return audit_doc(rows, max_age=511,
                         deadlines={"0": 256, "1": 256}, late_budget=256)

    def test_full_candidates_endpoints_and_late_bounds(self):
        report = run_audit(self.sample_doc())
        self.assertEqual(report["status"], "pairable")
        pairing = report["pairing"]
        self.assertEqual(len(report["request_events"]), 256)
        self.assertEqual(len(report["response_events"]), 256)
        self.assertEqual(pairing["confirmed_pairs"], [])
        # Every request really has all 256 feasible responses.
        self.assertEqual({len(row["candidates"]) for row in pairing["candidates"]}, {256})
        first = pairing["candidates"][0]
        self.assertEqual([c["cycle"] for c in first["candidates"]],
                         list(range(256, 512)))
        late_range = pairing["late_range"]
        self.assertEqual((late_range["minimum"], late_range["maximum"]), (0, 255))

        def late_count(witness):
            return sum(1 for pair in witness
                       if pair["response_cycle"] - pair["request_cycle"] > 256)

        self.assertEqual(len(late_range["minimum_witness"]), 256)
        self.assertEqual(len(late_range["maximum_witness"]), 256)
        self.assertEqual(late_count(late_range["minimum_witness"]), 0)
        self.assertEqual(late_count(late_range["maximum_witness"]), 255)
        # Witnesses use every request and every response exactly once.
        for witness in (late_range["minimum_witness"], late_range["maximum_witness"]):
            self.assertEqual(len({p["request_cycle"] for p in witness}), 256)
            self.assertEqual(len({p["response_cycle"] for p in witness}), 256)
        # Endpoint evidence cross-checks against the event tables.
        for pair in late_range["maximum_witness"]:
            self.assertTrue(0 <= pair["request_cycle"] < 256)
            self.assertTrue(256 <= pair["response_cycle"] < 512)

    def test_cli_sample_under_60_seconds_and_256_mib(self):
        fixture = Path("fixtures/audit/handoff-512.json")
        if not fixture.exists():
            self.skipTest("generated fixture missing; run tools/generate_audit_handoff.py")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.json"
            env = dict(os.environ)
            # RLIMIT_AS = 256 MiB on the whole CLI process.
            rlimit_as = 256 * 1024 * 1024
            start = time.perf_counter()
            completed = subprocess.run(
                [sys.executable, "-I", "-m", "logic_replay.cli",
                 "--audit-transfers", str(fixture), "--output", str(output)],
                env=env, capture_output=True, text=True,
                preexec_fn=lambda: resource.setrlimit(
                    resource.RLIMIT_AS, (rlimit_as, rlimit_as)))
            elapsed = time.perf_counter() - start
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertLess(elapsed, 60)
            report = json.loads(output.read_text())
            self.assertEqual(
                (report["pairing"]["late_range"]["minimum"],
                 report["pairing"]["late_range"]["maximum"]), (0, 255))


if __name__ == "__main__":
    unittest.main()
