"""Shard archiving (v5) and center-side batch rollup with corrections.

A station packs one contiguous clock slice without knowing the preceding
state: every complete three-valued flip-flop entry state is simulated
independently (an explicit ``"X"`` stays one literal value, never two
binary candidates, and distinct entries are never merged into one
executable branch).  The archive keeps per-branch conditional end states,
expectation accounting, and per-gate value sequences with seam boundaries,
but neither raw per-cycle stimuli, replayable vectors, full per-cycle
state/output traces, nor an executable netlist.

The center stitches archives by ``first_cycle``, selects each shard's
branch from the real prefix end state (starting from the netlist initial
values, never resetting), and emits full-batch summaries plus precise
add/remove/update correction events keyed by ``(cycle, output)``.
"""
import hashlib
import itertools
import json
import re
from pathlib import Path

from .gates import evaluate
from .io import fields, integer, label, load, rows
from .netlist import BITS, CONSTANTS, check_vectors, compile_circuit, net

SHARD_SCHEMA = "logic-replay-shard-v5"
ROLLUP_SCHEMA = "logic-replay-rollup-v5"
SCHEMA_VERSION = 5
MAX_FLOPS = 3
MAX_OUTPUTS = 16
MAX_SHARD_VECTORS = 32
MAX_CYCLE = 511
MAX_BATCH_CYCLES = 512
MAX_BASE = 16
MAX_REPLACE = 16
MAX_PATH = 4096
VALUE_ORDER = ("0", "1", "X")


def _canonical(payload):
    """Deterministic byte encoding used for fingerprints and checksums."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _full_hex(value):
    return re.fullmatch(r"[0-9a-f]{64}", value) is not None


def netlist_fingerprint(inputs, gates, flops, outputs):
    """Order-insensitive identity digest.

    Object key order and the declaration order of inputs, gates, flops and
    observed outputs do not matter; gate argument arrays keep their original
    order, as do the fields inside one gate/flop record.
    """
    payload = {
        "inputs": sorted(inputs),
        "gates": sorted(({"id": gid, "op": gate["op"], "args": list(gate["args"])}
                         for gid, gate in gates.items()), key=lambda row: row["id"]),
        "flops": sorted(({"id": fid, "d": flop["d"], "initial": flop["initial"]}
                         for fid, flop in flops.items()), key=lambda row: row["id"]),
        "outputs": sorted(outputs),
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _domain(flop_names):
    """All complete three-valued entry states in a fixed enumeration order."""
    if not flop_names:
        return [()]
    return list(itertools.product(VALUE_ORDER, repeat=len(flop_names)))


def _simulate_branch(spec, vectors, entry):
    """Replay one shard for one concrete flip-flop entry state tuple."""
    names = spec["flop_names"]
    state = dict(zip(names, entry))
    gate_values = {gid: [] for gid in spec["order"]}
    mismatches, comparisons = [], 0
    for index, (vector_id, vector) in enumerate(vectors.items()):
        values = {**CONSTANTS, **vector["inputs"], **state}
        for gid in spec["order"]:
            gate = spec["gates"][gid]
            values[gid] = evaluate(gate["op"], [values[arg] for arg in gate["args"]])
            gate_values[gid].append(values[gid])
        observed = {name: values[name] for name in spec["outputs"]}
        for output in sorted(vector["expect"]):
            expected = vector["expect"][output]
            if expected == "*":  # wildcard and missing expectations do not compare
                continue
            comparisons += 1
            actual = observed[output]
            if expected != actual:
                mismatches.append({"cycle": spec["first_cycle"] + index,
                                   "vector_id": vector_id, "output": output,
                                   "expected": expected, "actual": actual})
        state = {name: values[spec["flops"][name]["d"]] for name in names}
    return state, comparisons, mismatches, gate_values


def run_pack_shard(data):
    """Validate one station slice and produce its self-contained archive."""
    fields(data, ("stream_id", "shard_id", "first_cycle", "circuit", "vectors"))
    stream_id = label(data["stream_id"], "stream_id")
    shard_id = label(data["shard_id"], "shard_id")
    first_cycle = integer(data["first_cycle"], "first_cycle", 0, MAX_CYCLE)
    circuit = data["circuit"]
    if not isinstance(circuit, dict):
        raise ValueError("circuit must be an object")
    fields(circuit, ("inputs", "gates", "flops", "outputs"))
    inputs, gates, flops, outputs, order = compile_circuit(circuit)
    if len(flops) > MAX_FLOPS:
        raise ValueError(f"pack shard allows at most {MAX_FLOPS} flops")
    if len(outputs) > MAX_OUTPUTS:
        raise ValueError(f"pack shard allows at most {MAX_OUTPUTS} observed outputs")
    vectors = check_vectors(data["vectors"], inputs, outputs, 1, MAX_SHARD_VECTORS)
    cycle_count = len(vectors)
    if first_cycle + cycle_count - 1 > MAX_CYCLE:
        raise ValueError(f"shard coverage must stay within cycles 0..{MAX_CYCLE}")

    flop_names = sorted(flops)
    spec = {"gates": gates, "flops": flops, "outputs": outputs, "order": order,
            "flop_names": flop_names, "first_cycle": first_cycle}
    branches = []
    for entry in _domain(flop_names):
        end_state, comparisons, mismatches, gate_values = _simulate_branch(
            spec, vectors, entry)
        branch = {
            "entry": dict(zip(flop_names, entry)),
            "key": "".join(entry),
            "end_state": {name: end_state[name] for name in flop_names},
            "comparisons": comparisons,
            "mismatches": mismatches,
            "gates": {gid: {"first": seq[0], "last": seq[-1], "values": seq}
                      for gid, seq in gate_values.items()},
        }
        branches.append(branch)

    archive = {
        "schema": SHARD_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "stream_id": stream_id,
        "shard_id": shard_id,
        "first_cycle": first_cycle,
        "cycle_count": cycle_count,
        "vector_ids": list(vectors),
        "netlist": {
            "fingerprint": netlist_fingerprint(inputs, gates, flops, outputs),
            "inputs": sorted(inputs),
            "gate_ids": sorted(gates),
            "flop_ids": flop_names,
            "flop_initial": {name: flops[name]["initial"] for name in flop_names},
            "outputs": sorted(outputs),
        },
        "entry_states": branches,
    }
    archive["summary"] = {"stream_id": stream_id, "shard_id": shard_id,
                          "first_cycle": first_cycle, "cycles": cycle_count,
                          "gates": len(gates), "flops": len(flops),
                          "entry_states": len(branches)}
    archive["checksum"] = "sha256:" + hashlib.sha256(
        _canonical({key: value for key, value in archive.items()
                    if key != "checksum"})).hexdigest()
    return archive


# --------------------------------------------------------------------------- #
# Archive reading and internal consistency validation
# --------------------------------------------------------------------------- #

def _name_list(value, label_text, minimum, maximum):
    names = rows(value, label_text, minimum, maximum)
    result = []
    for name in names:
        result.append(net(name))
    if len(set(result)) != len(result):
        raise ValueError(f"duplicate name in {label_text}")
    return sorted(result)


def _validate_archive(doc):
    """Validate one archive document and return a normalized in-memory form."""
    fields(doc, ("schema", "schema_version", "stream_id", "shard_id", "first_cycle",
                 "cycle_count", "vector_ids", "netlist", "entry_states", "summary",
                 "checksum"))
    if doc["schema"] != SHARD_SCHEMA or doc["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unknown shard archive format identifier")
    checksum = doc["checksum"]
    if not isinstance(checksum, str) or not checksum.startswith("sha256:"):
        raise ValueError("archive checksum must be a sha256: hex digest")
    expected = "sha256:" + hashlib.sha256(
        _canonical({key: value for key, value in doc.items()
                    if key != "checksum"})).hexdigest()
    if checksum != expected:
        raise ValueError("archive checksum mismatch")

    stream_id = label(doc["stream_id"], "stream_id")
    shard_id = label(doc["shard_id"], "shard_id")
    first_cycle = integer(doc["first_cycle"], "first_cycle", 0, MAX_CYCLE)
    cycle_count = integer(doc["cycle_count"], "cycle_count", 1, MAX_SHARD_VECTORS)
    if first_cycle + cycle_count - 1 > MAX_CYCLE:
        raise ValueError(f"shard coverage must stay within cycles 0..{MAX_CYCLE}")

    vector_ids = rows(doc["vector_ids"], "vector_ids", cycle_count, cycle_count)
    vectors = []
    for vector_id in vector_ids:
        vectors.append(label(vector_id, "vector_id"))
    if len(set(vectors)) != len(vectors):
        raise ValueError("duplicate vector id in archive")

    section = doc["netlist"]
    fields(section, ("fingerprint", "inputs", "gate_ids", "flop_ids",
                     "flop_initial", "outputs"))
    fingerprint = section["fingerprint"]
    if not isinstance(fingerprint, str) or not _full_hex(fingerprint):
        raise ValueError("netlist fingerprint must be a sha256 hex string")
    inputs = _name_list(section["inputs"], "netlist.inputs", 1, 64)
    gate_ids = _name_list(section["gate_ids"], "netlist.gate_ids", 0, 200)
    flop_ids = _name_list(section["flop_ids"], "netlist.flop_ids", 0, MAX_FLOPS)
    outputs = _name_list(section["outputs"], "netlist.outputs", 1, MAX_OUTPUTS)
    initial_raw = section["flop_initial"]
    if not isinstance(initial_raw, dict) or set(initial_raw) != set(flop_ids):
        raise ValueError("flop_initial must map every declared flop exactly once")
    flop_initial = {}
    for name in flop_ids:
        flop_initial[name] = _bit(initial_raw[name], f"flop_initial.{name}")

    branch_rows = rows(doc["entry_states"], "entry_states",
                       len(VALUE_ORDER) ** len(flop_ids),
                       len(VALUE_ORDER) ** len(flop_ids))
    expected_keys = {"".join(entry) for entry in _domain(flop_ids)}
    branches = {}
    output_set, gate_set = set(outputs), set(gate_ids)
    for row in branch_rows:
        fields(row, ("entry", "key", "end_state", "comparisons", "mismatches", "gates"))
        entry = row["entry"]
        if not isinstance(entry, dict) or set(entry) != set(flop_ids):
            raise ValueError("branch entry state must name every declared flop")
        entry_values = {name: _bit(entry[name], f"entry.{name}") for name in flop_ids}
        key = row["key"]
        if not isinstance(key, str) or key != "".join(entry_values[name]
                                                       for name in flop_ids):
            raise ValueError("branch key does not match its entry state")
        if key in branches or key not in expected_keys:
            raise ValueError("branch entry-state domain incomplete or duplicated")
        end_raw = row["end_state"]
        if not isinstance(end_raw, dict) or set(end_raw) != set(flop_ids):
            raise ValueError("branch end state must name every declared flop")
        end_state = {name: _bit(end_raw[name], f"end_state.{name}")
                     for name in flop_ids}
        comparisons = integer(row["comparisons"], "comparisons", 0,
                              cycle_count * len(outputs))
        mismatch_rows = rows(row["mismatches"], "mismatches", 0,
                             cycle_count * len(outputs))
        mismatches = []
        for mismatch in mismatch_rows:
            fields(mismatch, ("cycle", "vector_id", "output", "expected", "actual"))
            cycle = integer(mismatch["cycle"], "mismatch.cycle",
                            first_cycle, first_cycle + cycle_count - 1)
            mid = label(mismatch["vector_id"], "mismatch.vector_id")
            if mid != vectors[cycle - first_cycle]:
                raise ValueError("mismatch vector id inconsistent with cycle position")
            output = net(mismatch["output"])
            if output not in output_set:
                raise ValueError("mismatch references undeclared output")
            expected = _bit(mismatch["expected"], "mismatch.expected")
            actual = _bit(mismatch["actual"], "mismatch.actual")
            mismatches.append({"cycle": cycle, "vector_id": mid, "output": output,
                               "expected": expected, "actual": actual})
        if len(mismatches) > comparisons:
            raise ValueError("branch cannot record more mismatches than comparisons")
        gate_section = row["gates"]
        if not isinstance(gate_section, dict) or set(gate_section) != gate_set:
            raise ValueError("branch gate records must cover every declared gate once")
        gate_records = {}
        for gid in gate_ids:
            record = gate_section[gid]
            fields(record, ("first", "last", "values"))
            seq = rows(record["values"], f"gates.{gid}.values",
                       cycle_count, cycle_count)
            values = [_bit(value, f"gates.{gid}.values") for value in seq]
            first = _bit(record["first"], f"gates.{gid}.first")
            last = _bit(record["last"], f"gates.{gid}.last")
            if first != values[0] or last != values[-1]:
                raise ValueError(f"gate {gid} boundary values inconsistent with sequence")
            gate_records[gid] = {"first": first, "last": last, "values": values}
        branches[key] = {"entry": entry_values, "end_state": end_state,
                         "comparisons": comparisons,
                         "mismatches": sorted(mismatches,
                                              key=lambda item: (item["cycle"],
                                                                item["output"])),
                         "gates": gate_records}
    if set(branches) != expected_keys:
        raise ValueError("branch entry-state domain incomplete")

    summary = doc["summary"]
    fields(summary, ("stream_id", "shard_id", "first_cycle", "cycles",
                     "gates", "flops", "entry_states"))
    if (summary["stream_id"] != stream_id or summary["shard_id"] != shard_id
            or summary["first_cycle"] != first_cycle
            or summary["cycles"] != cycle_count
            or summary["gates"] != len(gate_ids)
            or summary["flops"] != len(flop_ids)
            or summary["entry_states"] != len(branches)):
        raise ValueError("archive summary inconsistent with its declared records")

    return {"stream_id": stream_id, "shard_id": shard_id,
            "first_cycle": first_cycle, "cycle_count": cycle_count,
            "vector_ids": vectors, "fingerprint": fingerprint, "inputs": inputs,
            "gate_ids": gate_ids, "flop_ids": flop_ids,
            "flop_initial": flop_initial, "outputs": outputs, "branches": branches}


def _bit(value, name):
    if not isinstance(value, str) or value not in BITS:
        raise ValueError(f'{name} must be string "0", "1" or "X"')
    return value


# --------------------------------------------------------------------------- #
# Center-side manifest handling
# --------------------------------------------------------------------------- #

def _path_text(value, name):
    if not isinstance(value, str) or not value or len(value) > MAX_PATH or "\x00" in value:
        raise ValueError(f"{name} requires a nonempty path string <= {MAX_PATH} characters")
    return value


def _resolve(base_dir, text):
    path = Path(text)
    if path.is_absolute():
        return text
    return str(base_dir / path)


def _validate_batch(archives, stream_id, cycle_count):
    """Cross-shard identity, uniqueness and contiguous coverage checks."""
    fingerprints = {archive["fingerprint"] for archive in archives}
    if len(fingerprints) != 1:
        raise ValueError("shards mix different netlists")
    if any(archive["stream_id"] != stream_id for archive in archives):
        raise ValueError("shards mix different stream ids")
    shard_ids = [archive["shard_id"] for archive in archives]
    if len(set(shard_ids)) != len(shard_ids):
        raise ValueError("duplicate shard id in base manifest")
    all_vector_ids = []
    for archive in archives:
        all_vector_ids.extend(archive["vector_ids"])
    if len(set(all_vector_ids)) != len(all_vector_ids):
        raise ValueError("vector id must be unique across the whole batch")
    ordered = sorted(archives, key=lambda archive: archive["first_cycle"])
    if ordered[0]["first_cycle"] != 0:
        raise ValueError("batch coverage must start at cycle 0")
    expected_first = 0
    for archive in ordered:
        if archive["first_cycle"] != expected_first:
            raise ValueError("shard coverage has a gap or overlap")
        expected_first = archive["first_cycle"] + archive["cycle_count"]
    if expected_first != cycle_count:
        raise ValueError("shards do not exactly cover the declared cycle_count")
    prototype = ordered[0]
    for archive in ordered[1:]:
        if (archive["inputs"] != prototype["inputs"]
                or archive["gate_ids"] != prototype["gate_ids"]
                or archive["flop_ids"] != prototype["flop_ids"]
                or archive["flop_initial"] != prototype["flop_initial"]
                or archive["outputs"] != prototype["outputs"]):
            raise ValueError("shards declare different netlist interfaces")
    return ordered


def _validate_replacement(old, new, stream_id):
    if new["shard_id"] != old["shard_id"]:
        raise ValueError("replacement must keep the same shard id")
    if new["stream_id"] != stream_id:
        raise ValueError("replacement stream id differs from manifest")
    if new["fingerprint"] != old["fingerprint"]:
        raise ValueError("replacement netlist identity differs")
    if (new["first_cycle"] != old["first_cycle"]
            or new["cycle_count"] != old["cycle_count"]):
        raise ValueError("replacement must keep the same covered cycle range")
    if new["vector_ids"] != old["vector_ids"]:
        raise ValueError("replacement must keep cycle-ordered vector ids")


def _summarize(ordered):
    """Stitch selected branches and produce one full-batch v1-equivalent summary."""
    head = ordered[0]
    flop_names = head["flop_ids"]
    state = {name: head["flop_initial"][name] for name in flop_names}
    comparisons = 0
    mismatches = []
    sequences = {gid: [] for gid in head["gate_ids"]}
    for archive in ordered:
        key = "".join(state[name] for name in flop_names)
        branch = archive["branches"][key]
        comparisons += branch["comparisons"]
        mismatches.extend(branch["mismatches"])
        for gid in head["gate_ids"]:
            sequences[gid].extend(branch["gates"][gid]["values"])
        state = {name: branch["end_state"][name] for name in flop_names}
    mismatches.sort(key=lambda item: (item["cycle"], item["output"]))
    gate_activity = {}
    for gid in head["gate_ids"]:
        sequence = sequences[gid]
        transitions = 0
        for previous, current in zip(sequence, sequence[1:]):
            if previous in ("0", "1") and current in ("0", "1") and previous != current:
                transitions += 1  # X on either side blocks; never look past an X
        gate_activity[gid] = {"values_seen": sorted(set(sequence)),
                              "known_transitions": transitions}
    return {"final_state": {name: state[name] for name in flop_names},
            "comparisons": comparisons,
            "mismatch_count": len(mismatches),
            "mismatches": mismatches,
            "gate_activity": gate_activity}


def _correction_events(old_summary, new_summary):
    old_map = {(item["cycle"], item["output"]): item
               for item in old_summary["mismatches"]}
    new_map = {(item["cycle"], item["output"]): item
               for item in new_summary["mismatches"]}
    events = []
    for cycle, output in sorted(set(old_map) | set(new_map)):
        old_item, new_item = old_map.get((cycle, output)), new_map.get((cycle, output))
        if old_item is None:
            events.append({"action": "add", "cycle": cycle, "output": output,
                           "record": new_item})
        elif new_item is None:
            events.append({"action": "remove", "cycle": cycle, "output": output,
                           "record": old_item})
        elif (old_item["expected"] != new_item["expected"]
              or old_item["actual"] != new_item["actual"]):
            events.append({"action": "update", "cycle": cycle, "output": output,
                           "old_record": old_item, "new_record": new_item})
    return events


def rollup_documents(stream_id, cycle_count, base_docs, replacement_docs):
    """Core rollup over already-loaded archive documents.

    ``base_docs`` are validated archive dicts in any listing order;
    ``replacement_docs`` maps shard id to its validated replacement archive.
    """
    archives = list(base_docs)
    ordered = _validate_batch(archives, stream_id, cycle_count)
    by_id = {archive["shard_id"]: archive for archive in archives}
    for shard_id, candidate in replacement_docs.items():
        if shard_id not in by_id:
            raise ValueError(f"replacement targets unknown shard: {shard_id}")
        _validate_replacement(by_id[shard_id], candidate, stream_id)
    old_summary = _summarize(ordered)
    new_order = [replacement_docs.get(archive["shard_id"], archive)
                 for archive in ordered]
    new_summary = _summarize(new_order)
    events = _correction_events(old_summary, new_summary)
    return {
        "schema": ROLLUP_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "stream_id": stream_id,
        "cycle_count": cycle_count,
        "replacements": sorted(replacement_docs),
        "old_summary": old_summary,
        "new_summary": new_summary,
        "corrections": events,
        "summary": {
            "cycle_count": cycle_count,
            "shards": len(ordered),
            "replacements": len(replacement_docs),
            "comparisons_old": old_summary["comparisons"],
            "comparisons_new": new_summary["comparisons"],
            "mismatches_old": old_summary["mismatch_count"],
            "mismatches_new": new_summary["mismatch_count"],
            "correction_events": len(events),
        },
    }


def run_rollup_shards(data, manifest_path):
    """Load a center manifest from disk, then build summaries and corrections."""
    fields(data, ("stream_id", "cycle_count", "base", "replace"))
    stream_id = label(data["stream_id"], "stream_id")
    cycle_count = integer(data["cycle_count"], "cycle_count", 1, MAX_BATCH_CYCLES)
    base_paths = rows(data["base"], "base", 1, MAX_BASE)
    base_dir = Path(manifest_path).parent
    base_docs = [_validate_archive(load(_resolve(base_dir, _path_text(path, "base path"))))
                 for path in base_paths]

    replacement_rows = rows(data["replace"], "replace", 0, MAX_REPLACE)
    replacement_docs, replaced_ids = {}, set()
    for row in replacement_rows:
        fields(row, ("shard_id", "path"))
        shard_id = label(row["shard_id"], "replace.shard_id")
        if shard_id in replaced_ids:
            raise ValueError(f"duplicate replacement for shard: {shard_id}")
        replaced_ids.add(shard_id)
        candidate = _validate_archive(load(
            _resolve(base_dir, _path_text(row["path"], "replace.path"))))
        replacement_docs[shard_id] = candidate
    return rollup_documents(stream_id, cycle_count, base_docs, replacement_docs)
