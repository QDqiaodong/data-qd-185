"""Strict bounded input and create-only publication shared by local CLI modules."""
import json
import os
import tempfile
from pathlib import Path


def reject_constant(value):
    raise ValueError(f"non-finite JSON value: {value}")


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load(path):
    with Path(path).open("rb") as source:
        raw = source.read(8_000_001)
    if len(raw) > 8_000_000:
        raise ValueError("source exceeds 8 MB")
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("root must be an object")
    return value


def fields(value, required, optional=()):
    if not isinstance(value, dict):
        raise ValueError("expected object")
    missing = set(required) - set(value)
    extra = set(value) - set(required) - set(optional)
    if missing or extra:
        raise ValueError(f"invalid fields: missing={sorted(missing)}, unknown={sorted(extra)}")


def integer(value, label, minimum=0, maximum=10**12):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} requires integer in [{minimum},{maximum}]")
    return value


def label(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError(f"{name} requires nonempty string <=200 characters")
    return value


def rows(value, name, minimum=0, maximum=1000):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{name} requires array of {minimum}..{maximum} rows")
    return value


def records(value, name, minimum=0, maximum=1000):
    result = {}
    for row in rows(value, name, minimum, maximum):
        if not isinstance(row, dict):
            raise ValueError(f"{name} row must be an object")
        key = label(row.get("id"), f"{name}.id")
        if key in result:
            raise ValueError(f"duplicate {name} ID: {key}")
        result[key] = row
    return result


def publish(path, data):
    encoded = (json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".report-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
        os.link(temporary, target)
    finally:
        os.unlink(temporary)
