"""Placeholders in strings of the wiring: `{run}`, `{run.<field>}`, `{unit}`, `{unit.<field>}`, `{<step>.<field>}`."""

from __future__ import annotations

import re
from collections.abc import Callable

PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


class Unresolved(Exception):
    """A placeholder that has no value yet, or none at all."""


def render(value, lookup: Callable[[str], object]):
    """`value` with every placeholder in its strings replaced by `lookup(name)`, recursively."""
    if isinstance(value, dict):
        return {k: render(v, lookup) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, lookup) for v in value]
    if not isinstance(value, str):
        return value
    whole = PLACEHOLDER.fullmatch(value)
    if whole:  # a lone placeholder keeps its value's type, so `{unit.files}` can be a list
        return lookup(whole.group(1))
    return PLACEHOLDER.sub(lambda m: str(lookup(m.group(1))), value)
