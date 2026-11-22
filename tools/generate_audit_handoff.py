#!/usr/bin/env python3
"""Generate the drained-capture minute-handoff acceptance sample.

512 contiguous vectors with no flip-flops: the first 256 rows each confirm a
request on one tag/class, the last 256 each confirm a response on the same
tag, while the other channel never handshakes.  Vector IDs are short serial
numbers.  With max_age=511 and both class deadlines at 256 every request has
256 feasible responses and the achievable late range is [0, 255].

Run (offline, standard library only):

    python3 -I tools/generate_audit_handoff.py fixtures/audit/handoff-512.json

The target path must not already exist.
"""
import json
import sys
from pathlib import Path

SIGNALS = ["rv", "rr", "rq0", "rc", "sv", "sr", "sq0"]


def build_document():
    rows = []
    for cycle in range(512):
        values = dict(zip(SIGNALS, ["0"] * len(SIGNALS)))
        if cycle < 256:
            values.update(rv="1", rr="1", rq0="0", rc="0")
        else:
            values.update(sv="1", sr="1", sq0="0")
        rows.append(values)
    source = {
        "inputs": list(SIGNALS),
        "gates": [{"id": f"buf_{name}", "op": "BUF", "args": [name]}
                  for name in SIGNALS],
        "flops": [],
        "outputs": list(SIGNALS),
        "vectors": [
            {"id": f"v{cycle:03d}", "inputs": rows[cycle], "expect": {}}
            for cycle in range(512)
        ],
    }
    return {
        "source": source,
        "request": {"valid": "rv", "ready": "rr", "tag_bits": ["rq0"],
                    "class_bit": "rc"},
        "response": {"valid": "sv", "ready": "sr", "tag_bits": ["sq0"]},
        "max_age": 511,
        "deadlines": {"0": 256, "1": 256},
        "late_budget": 256,
    }


def main(argv):
    if len(argv) != 2:
        print("usage: generate_audit_handoff.py <output-json>", file=sys.stderr)
        return 2
    target = Path(argv[1])
    if target.exists() or target.is_symlink():
        print(f"refusing to overwrite existing target: {target}", file=sys.stderr)
        return 2
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(build_document(), ensure_ascii=False, indent=2) + "\n")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
