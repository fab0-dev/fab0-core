"""The ledger: one YAML file per attempt of a unit, written by `record`, carried to the base branch by merges."""

from __future__ import annotations

import re

import yaml

from .errors import EXIT_ERROR, Fail
from .schemas import core_schema, schema_errors

DEFAULT_PATH = "work/{run}/ledger"


def entry_name(unit: str, attempt: int, gate: bool = False) -> str:
    return f"{unit}-{attempt}{'-gate' if gate else ''}.yaml"


def attempts(names: set[str], unit: str) -> list[int]:
    """The attempts of `unit` among ledger file names, oldest first."""
    pattern = re.compile(rf"^{re.escape(unit)}-([1-9][0-9]*)\.yaml$")
    return sorted(int(m.group(1)) for n in names if (m := pattern.match(n)))


def gate_attempts(names: set[str], unit: str) -> list[int]:
    """The attempts of `unit` that an auto gate has decided on."""
    pattern = re.compile(rf"^{re.escape(unit)}-([1-9][0-9]*)-gate\.yaml$")
    return sorted(int(m.group(1)) for n in names if (m := pattern.match(n)))


def parse(raw: bytes | str, where: str) -> dict:
    try:
        entry = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise Fail(EXIT_ERROR, f"invalid ledger entry {where}: {exc}") from None
    errors = schema_errors(entry, core_schema("ledger-entry"))
    if errors:
        raise Fail(EXIT_ERROR, f"invalid ledger entry {where}", errors=errors)
    return entry


def dump(entry: dict) -> str:
    return yaml.safe_dump(entry, sort_keys=False, allow_unicode=True)
