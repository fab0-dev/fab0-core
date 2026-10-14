"""fab0-resolve --version, and the two version declarations."""

import json
import shutil
import subprocess
import sys

import pytest
from conftest import ROOT, SCRIPT

PLUGIN = ROOT / "plugins" / "fab0"
VERSION = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["version"]


def run_version(script, cwd):
    run = subprocess.run([sys.executable, str(script), "--version"], cwd=cwd, capture_output=True, text=True)
    return run.returncode, json.loads(run.stdout)


def broken_plugin(tmp_path, manifest):
    """A copy of the plugin whose manifest is `manifest` (None: missing)."""
    root = tmp_path / "plugin"
    shutil.copytree(PLUGIN, root)
    path = root / ".claude-plugin" / "plugin.json"
    if manifest is None:
        path.unlink()
    else:
        path.write_text(manifest)
    return root / "bin" / "fab0-resolve", path


def test_s1_version_from_anywhere(tmp_path):
    code, out = run_version(SCRIPT, tmp_path)  # no project, no git
    assert code == 0 and out == {"ok": True, "fab0_version": VERSION}


@pytest.mark.parametrize(
    "manifest", [None, "{not json", "[]", "{}", '{"version": 2}', '{"version": ""}', '{"version": "  "}']
)
def test_s2_unreadable_version(tmp_path, manifest):
    script, path = broken_plugin(tmp_path, manifest)
    code, out = run_version(script, tmp_path)
    assert code == 1 and out["ok"] is False
    assert str(path) in out["error"]


def test_s7_marketplace_and_manifest_agree():
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    entry = next(p for p in market["plugins"] if p["name"] == "fab0")
    assert entry["version"] == VERSION, (
        f"marketplace.json says {entry['version']}, plugins/fab0/.claude-plugin/plugin.json says {VERSION}"
    )
