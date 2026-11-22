#!/usr/bin/env python3
"""Generate the soak-log endurance handoff acceptance sample (schema v7).

Two holds cover one trillion clocks without ever expanding a per-cycle
vector file: hold A keeps a="1" for 800000000001 ticks, hold B keeps a="0"
for 199999999999 ticks.  The flip-flop q (initial "0") follows q <- XOR(q, a),
so q toggles through A and stays constant through B.

Two probes observe q and expect "0" with through=999999999999:
  * dense  samples every clock from 0, trip_after=2 -> 1000000000000 samples,
    599999999999 mismatches, first trip at 800000000002;
  * sparse samples every 200000000 clocks from 1, trip_after=4001 -> 5000
    samples, all mismatching, first trip at 800000000001.
The earliest protection trip is 800000000001 (sparse); capture before=2,
after=1 freezes clocks 799999999999..800000000002 with pre-edge q
"1", "0", "1", "1".

Run (offline, standard library only):

    python3 -I tools/generate_soak_handoff.py fixtures/soak/handoff-1e12.json

The target path must not already exist.
"""
import json
import sys
from pathlib import Path


def build_document():
    return {
        "circuit": {
            "inputs": ["a"],
            "gates": [{"id": "g", "op": "XOR", "args": ["q", "a"]}],
            "flops": [{"id": "q", "d": "g", "initial": "0"}],
            "outputs": ["q"],
        },
        "holds": [
            {"id": "A", "ticks": 800000000001, "inputs": {"a": "1"}},
            {"id": "B", "ticks": 199999999999, "inputs": {"a": "0"}},
        ],
        "monitors": [
            {"id": "dense", "output": "q", "first": 0,
             "through": 999999999999, "stride": 1,
             "expected": "0", "trip_after": 2},
            {"id": "sparse", "output": "q", "first": 1,
             "through": 999999999999, "stride": 200000000,
             "expected": "0", "trip_after": 4001},
        ],
        "capture": {"before": 2, "after": 1},
    }


def main(argv):
    if len(argv) != 2:
        print("usage: generate_soak_handoff.py <output JSON>", file=sys.stderr)
        return 2
    target = Path(argv[1])
    if target.exists() or target.is_symlink():
        print(f"refusing to overwrite existing path: {target}", file=sys.stderr)
        return 2
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(build_document(), ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
