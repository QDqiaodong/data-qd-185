"""Cross-version qualification (schema v3).

课程测试向量是已发布的行为契约。本模块在同一组向量上对比参考版与候选版
两个网表：确认参考版确实符合契约，把候选版的偏差区分为经课程负责人授权
的有意变更与意外回归，决定候选版是否取得可发布资格。
"""
from .io import fields, label, rows
from .netlist import BITS, check_vectors, compile_circuit, net
from .simulate import replay

APPROVAL_FIELDS = ("reference_revision", "candidate_revision", "vector_id",
                   "output", "reference_value", "candidate_value", "ticket", "reason")


def compile_version(data, name):
    fields(data, ("revision", "netlist"))
    revision = label(data["revision"], f"{name}.revision")
    netlist = data["netlist"]
    fields(netlist, ("inputs", "gates", "flops", "outputs"))
    inputs, gates, flops, outputs, order = compile_circuit(netlist)
    return {"revision": revision, "inputs": inputs, "gates": gates,
            "flops": flops, "outputs": outputs, "order": order}


def compile_qualify(data):
    """Validate the whole qualification input before any replay happens."""
    fields(data, ("reference", "candidate", "vectors"))
    reference = compile_version(data["reference"], "reference")
    candidate = compile_version(data["candidate"], "candidate")
    if reference["revision"] == candidate["revision"]:
        raise ValueError("reference and candidate revisions must differ")
    if set(reference["inputs"]) != set(candidate["inputs"]):
        raise ValueError("versions disagree on input net interface")
    if set(reference["outputs"]) != set(candidate["outputs"]):
        raise ValueError("versions disagree on observed output interface")
    vectors = check_vectors(data["vectors"], reference["inputs"],
                            reference["outputs"], 1, 1000)
    return {"reference": reference, "candidate": candidate, "vectors": vectors}


def nonempty_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} requires a string nonempty after trimming")
    return value


def compile_approvals(data):
    """Structural validation only; matching against run data is a business step."""
    fields(data, ("approvals",))
    approvals, seen = [], set()
    for row in rows(data["approvals"], "approvals", 0, 1000):
        fields(row, APPROVAL_FIELDS)
        approval = {
            "reference_revision": label(row["reference_revision"], "approval.reference_revision"),
            "candidate_revision": label(row["candidate_revision"], "approval.candidate_revision"),
            "vector_id": label(row["vector_id"], "approval.vector_id"),
            "output": net(row["output"]),
            "reference_value": row["reference_value"],
            "candidate_value": row["candidate_value"],
            "ticket": nonempty_text(row["ticket"], "approval.ticket"),
            "reason": nonempty_text(row["reason"], "approval.reason"),
        }
        for key in ("reference_value", "candidate_value"):
            if not isinstance(approval[key], str) or approval[key] not in BITS:
                raise ValueError(f"approval.{key} must be string 0, 1 or X")
        if approval["reference_value"] == approval["candidate_value"]:
            raise ValueError("approval values must differ")
        key = (approval["reference_revision"], approval["candidate_revision"],
               approval["vector_id"], approval["output"])
        if key in seen:
            raise ValueError(f"duplicate approval: {key}")
        seen.add(key)
        approvals.append(approval)
    return approvals


def trace_view(trace):
    return [{"cycle": row["cycle"], "vector_id": row["vector_id"],
             "state_before_edge": row["state_before_edge"],
             "outputs_before_edge": row["outputs_before_edge"],
             "state_after_edge": row["state_after_edge"]}
            for row in trace]


def reconcile(spec, ref_trace, cand_trace):
    """Compare both replays cell by cell.

    Returns (cells, differences, reference_contract): one cell per vector/
    output pair carrying an explicit (non-wildcard) expectation, in replay
    order; one difference row per observed pair whose actual values differ,
    regardless of expectations.
    """
    vectors, outputs = spec["vectors"], spec["reference"]["outputs"]
    cells, differences = [], []
    reference_contract = "valid"
    for cycle, (vector_id, vector) in enumerate(vectors.items()):
        ref_observed = ref_trace[cycle]["outputs_before_edge"]
        cand_observed = cand_trace[cycle]["outputs_before_edge"]
        for output in outputs:
            ref_value = ref_observed[output]
            cand_value = cand_observed[output]
            if ref_value != cand_value:
                differences.append({"cycle": cycle, "vector_id": vector_id,
                                    "output": output, "reference_value": ref_value,
                                    "candidate_value": cand_value})
            expected = vector["expect"].get(output)
            if expected is None or expected == "*":
                continue
            cell = {"cycle": cycle, "vector_id": vector_id, "output": output,
                    "expected": expected, "reference_value": ref_value,
                    "candidate_value": cand_value,
                    "reference_matches": ref_value == expected,
                    "candidate_matches": cand_value == expected,
                    "versions_differ": ref_value != cand_value}
            cells.append(cell)
            if not cell["reference_matches"]:
                reference_contract = "invalid"
    return cells, differences, reference_contract


def disposition_approvals(spec, cells, approvals):
    """Match each approval against this run's data.

    Returns (matched, stale): `matched` maps (vector_id, output) to the
    approval exactly matching an authorizable deviation; `stale` lists
    well-formed approvals that do not match, in approvals-file order.
    """
    ref_revision = spec["reference"]["revision"]
    cand_revision = spec["candidate"]["revision"]
    vectors = spec["vectors"]
    output_set = set(spec["reference"]["outputs"])
    cell_by_key = {(cell["vector_id"], cell["output"]): cell for cell in cells}
    matched, stale = {}, []
    for approval in approvals:
        detail = None
        if (approval["reference_revision"] != ref_revision
                or approval["candidate_revision"] != cand_revision):
            detail = "revision_mismatch"
        else:
            cell = cell_by_key.get((approval["vector_id"], approval["output"]))
            if cell is None:
                if approval["vector_id"] not in vectors:
                    detail = "unknown_vector"
                elif approval["output"] not in output_set:
                    detail = "unknown_output"
                else:
                    detail = "no_explicit_expectation"
            elif not (not cell["candidate_matches"] and cell["versions_differ"]):
                detail = "not_a_deviation"
            elif (approval["reference_value"] != cell["reference_value"]
                  or approval["candidate_value"] != cell["candidate_value"]):
                detail = "value_mismatch"
            else:
                matched[(approval["vector_id"], approval["output"])] = approval
        if detail is not None:
            stale.append({**approval, "detail": detail})
    return matched, stale


def run_qualify(data, approvals_data):
    spec = compile_qualify(data)
    approvals = compile_approvals(approvals_data)
    reference, candidate, vectors = spec["reference"], spec["candidate"], spec["vectors"]
    traces = {}
    for name, version in (("reference", reference), ("candidate", candidate)):
        result = replay(version["gates"], version["flops"], version["outputs"],
                        vectors, version["order"])
        traces[name] = result["trace"]
    cells, differences, reference_contract = reconcile(spec, traces["reference"],
                                                       traces["candidate"])
    matched, stale_approvals = disposition_approvals(spec, cells, approvals)

    reconciliation, approved_changes, unapproved_regressions = [], [], []
    for cell in cells:
        key = (cell["vector_id"], cell["output"])
        entry = {name: cell[name] for name in
                 ("cycle", "vector_id", "output", "expected",
                  "reference_value", "candidate_value")}
        if cell["candidate_matches"]:
            status = "match"
        elif key in matched:
            status = "approved_change"
            approval = matched[key]
            approved_changes.append({**entry, "ticket": approval["ticket"],
                                     "reason": approval["reason"]})
        else:
            status = "unapproved_regression"
            unapproved_regressions.append(entry)
        reconciliation.append({**cell, "status": status})

    eligible = (reference_contract == "valid"
                and not unapproved_regressions and not stale_approvals)
    ref_revision, cand_revision = reference["revision"], candidate["revision"]
    summary = {"reference_revision": ref_revision,
               "candidate_revision": cand_revision,
               "cycles": len(vectors),
               "comparisons": len(reconciliation),
               "reference_contract": reference_contract,
               "reference_mismatches": sum(1 for cell in cells
                                           if not cell["reference_matches"]),
               "candidate_mismatches": sum(1 for cell in cells
                                           if not cell["candidate_matches"]),
               "version_differences": len(differences),
               "approvals_total": len(approvals),
               "approved_changes": len(approved_changes),
               "unapproved_regressions": len(unapproved_regressions),
               "stale_approvals": len(stale_approvals),
               "eligible": eligible}
    return {"schema_version": 3,
            "reference_revision": ref_revision,
            "candidate_revision": cand_revision,
            "reference_contract": reference_contract,
            "eligible": eligible,
            "reference": {"revision": ref_revision,
                          "trace": trace_view(traces["reference"])},
            "candidate": {"revision": cand_revision,
                          "trace": trace_view(traces["candidate"])},
            "reconciliation": reconciliation,
            "differences": differences,
            "approved_changes": approved_changes,
            "unapproved_regressions": unapproved_regressions,
            "stale_approvals": stale_approvals,
            "summary": summary}
