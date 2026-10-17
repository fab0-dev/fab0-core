"""JSON Schemas: the core ones shipped with the plugin, and a project's own."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from .paths import PLUGIN_ROOT, SCHEMAS

_cache: dict[Path, dict] = {}


def load_schema(path: Path) -> dict:
    path = path.resolve()
    if path not in _cache:
        _cache[path] = json.loads(path.read_text())
    return _cache[path]


def core_schema(name: str) -> dict:
    """A core schema by name: `project`, `pipeline`, `step`, `artifact`, `verdict`, `return`, `ledger-entry`,
    `claim`, `event`."""
    return load_schema(SCHEMAS / f"{name}.json")


def schema_errors(instance, schema: dict, validator: jsonschema.Draft202012Validator | None = None) -> list[str]:
    """Readable errors, one per failing location, sorted by location. A caller that checks many instances
    against one schema passes its `validator`, built once."""
    validator = validator or jsonschema.Draft202012Validator(schema)
    out = []
    for err in sorted(validator.iter_errors(instance), key=lambda e: [str(p) for p in e.absolute_path]):
        if err.context:
            err = jsonschema.exceptions.best_match(err.context)
        where = "/".join(str(p) for p in err.absolute_path) or "(root)"
        out.append(f"{where}: {err.message}")
    return out


def resolve_schema_ref(ref: str, root: Path) -> Path:
    """`fab0:schemas/x.json` is this plugin's; a path without a prefix is relative to the home repo."""
    if ":" in ref:
        plugin, rel = ref.split(":", 1)
        if plugin != "fab0":
            raise ValueError(f"'{ref}': only fab0: and project-relative schema paths resolve")
        return PLUGIN_ROOT / rel
    return root / ref


def check_schema_file(ref: str, root: Path) -> str | None:
    """None if `ref` names a valid JSON Schema, otherwise what is wrong."""
    try:
        path = resolve_schema_ref(ref, root)
    except ValueError as exc:
        return str(exc)
    if not path.is_file():
        return f"{ref} not found"
    try:
        jsonschema.Draft202012Validator.check_schema(load_schema(path))
    except (json.JSONDecodeError, jsonschema.SchemaError) as exc:
        return f"{ref} is not a valid JSON Schema: {str(exc).splitlines()[0]}"
    return None
