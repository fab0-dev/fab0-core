"""The release of the plugin this engine belongs to."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .errors import EXIT_ERROR, Fail
from .paths import MANIFEST, PLUGIN_ROOT


def read_version(manifest: Path | None = None) -> str:
    """The release named in the plugin's manifest; reads nothing else."""
    manifest = manifest or MANIFEST
    try:
        data = json.loads(manifest.read_text())
    except OSError as e:
        raise Fail(EXIT_ERROR, f"cannot read the plugin manifest {manifest}: {e.strerror or e}") from None
    except ValueError as e:
        raise Fail(EXIT_ERROR, f"the plugin manifest {manifest} is not valid JSON: {e}") from None
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise Fail(EXIT_ERROR, f"the plugin manifest {manifest} has no non-empty string `version`")
    return version


RELEASE = re.compile(r"^\d+(\.\d+)*$")
SCOPE_RANK = {"local": 0, "project": 1, "user": 2}


def config_dir() -> Path:
    """Claude Code's configuration directory: CLAUDE_CONFIG_DIR, else ~/.claude."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".claude"


def parse_release(text) -> tuple[int, ...] | None:
    """A dotted numeric release as a tuple, or None for anything else."""
    if not isinstance(text, str) or not RELEASE.match(text.strip()):
        return None
    return tuple(int(part) for part in text.strip().split("."))


def _resolved(path) -> Path | None:
    try:
        return Path(path).resolve() if isinstance(path, str) and path else None
    except (OSError, RuntimeError):
        return None


def _entries(plugins: dict):
    for key, items in plugins.items():
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    yield key, item


def _install_dir(cache: Path, key: str, item: dict) -> Path | None:
    path = _resolved(item.get("installPath"))
    if path is None and "@" in key and isinstance(item.get("version"), str):
        name, market = key.split("@", 1)
        path = (cache / market / name / item["version"]).resolve()
    return path


def check_engine(project: Path, manifest: Path | None = None) -> list[str]:
    """Compare this engine's release with the plugin Claude Code has installed for `project`.

    Raises Fail when the engine is older. Returns warnings when the check could not be made. Reads local files
    only and never changes anything under the configuration directory."""
    config = config_dir()
    record = config / "plugins" / "installed_plugins.json"
    if not record.is_file():
        return []
    try:
        data = json.loads(record.read_text())
    except (OSError, ValueError) as e:
        return [f"the engine's release couldn't be checked: {record} is not readable JSON ({e})"]
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return [f"the engine's release couldn't be checked: {record} has no `plugins` object"]

    try:
        manifest_data = json.loads((manifest or MANIFEST).read_text())
        name = manifest_data["name"]
    except (OSError, ValueError, KeyError, TypeError):
        name = "fab0"
    cache = (config / "plugins" / "cache").resolve()
    market = None
    try:
        parts = PLUGIN_ROOT.relative_to(cache).parts
        if len(parts) >= 3:
            market = parts[0]
    except ValueError:
        pass

    home = project.resolve()
    best = None
    for key, item in _entries(plugins):
        plugin, _, key_market = key.partition("@")
        if plugin != name or (market is not None and key_market != market):
            continue
        scope = item.get("scope")
        if scope not in SCOPE_RANK:
            continue
        if scope != "user" and _resolved(item.get("projectPath")) != home:
            continue
        if best is None or SCOPE_RANK[scope] < SCOPE_RANK[best[1].get("scope")]:
            best = (key, item)
    if best is None:
        return []
    key, item = best

    try:
        engine = read_version(manifest)
    except Fail as e:
        return [f"the engine's release couldn't be checked: {e}"]
    installed = item.get("version")
    engine_release, installed_release = parse_release(engine), parse_release(installed)
    if engine_release is None or installed_release is None:
        bad = f"the engine's release '{engine}'" if engine_release is None else f"the installed release {installed!r}"
        return [f"the engine's release couldn't be checked: {bad} is not a dotted number"]
    if engine_release >= installed_release:
        return []

    installed_dir = _install_dir(cache, key, item)
    used = {d for k, i in _entries(plugins) if (d := _install_dir(cache, k, i)) is not None}
    fix = []
    if installed_dir is not None and installed_dir.parent.is_dir():
        for sibling in sorted(installed_dir.parent.iterdir()):
            if sibling.is_dir() and sibling.resolve() not in used:
                fix.append(f"remove the cached version {sibling.resolve()}, which no install uses")
    fix.append("start a new Claude Code session, so that it puts the installed version's engine first on the PATH")
    raise Fail(
        EXIT_ERROR,
        f"this engine is fab0 {engine} ({PLUGIN_ROOT}), older than fab0 {installed} "
        f"({installed_dir or 'path unknown'}), the plugin installed for this project",
        fix=fix,
    )
