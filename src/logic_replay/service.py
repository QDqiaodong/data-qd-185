from .netlist import compile_netlist
from .simulate import replay


def run(data):
    _, gates, flops, outputs, vectors, order = compile_netlist(data)
    result = replay(gates, flops, outputs, vectors, order)
    return {"schema_version": 1, "combinational_order": order, **result,
            "summary": {"cycles": len(vectors), "gates": len(gates), "flops": len(flops),
                        "comparisons": result["comparisons"], "mismatches": len(result["mismatches"]),
                        "all_expected_match": not result["mismatches"]}}
