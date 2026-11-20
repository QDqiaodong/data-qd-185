"""Conservative three-valued truth tables, not symbolic Boolean correlation."""


def evaluate(op, args):
    if op == "BUF":
        return args[0]
    if op == "NOT":
        return {"0": "1", "1": "0", "X": "X"}[args[0]]
    if op in ("AND", "NAND"):
        result = "0" if "0" in args else ("X" if "X" in args else "1")
        return evaluate("NOT", [result]) if op == "NAND" else result
    if op in ("OR", "NOR"):
        result = "1" if "1" in args else ("X" if "X" in args else "0")
        return evaluate("NOT", [result]) if op == "NOR" else result
    if op == "XOR":
        return "X" if "X" in args else str(int(args[0]) ^ int(args[1]))
    raise ValueError("unknown gate")
