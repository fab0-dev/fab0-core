"""fab0-resolve --version, the release named in hand-outs, and the two version declarations."""

import json
import os
import shutil
import subprocess
import sys

import pytest
from conftest import ROOT, SCRIPT

PLUGIN = ROOT / "plugins" / "fab0"
VERSION = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["version"]
LINE = f"Handed out by fab0 {VERSION}."


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


def test_s3_stage_brief_names_the_release(ws):
    res = ws.next()
    assert res["kind"] == "stage" and res["fab0_version"] == VERSION
    lines = res["brief"].splitlines()
    assert lines[lines.index(LINE) - 2].startswith("Run 412:")


def test_s3_task_stage_brief_names_the_release(ws):
    ws.do_spec()
    ws.do_plan()
    res = ws.next("--task", "T1", user="backend-dev")
    assert res["kind"] == "stage" and res["fab0_version"] == VERSION and LINE in res["brief"]


def test_s4_gate_brief_names_the_release(ws):
    ws.do_spec()
    ws.do_plan()
    res = ws.next("--task", "T1", user="backend-dev")
    ws.checkout(ws.api, res["branch"], res["start_from"])
    ws.write(ws.api, "src/booking.py", "x = 1\n")
    assert ws.record("implement", {"summary": "Done."}, task="T1")["verified"]
    ws.commit(ws.api, "implement T1")
    gate = ws.next("--task", "T1", user="backend-dev")
    assert gate["kind"] == "gate" and gate["fab0_version"] == VERSION and LINE in gate["brief"]


def test_s5_no_hand_out_without_a_release(ws, tmp_path):
    script, path = broken_plugin(tmp_path, "{}")
    run = subprocess.run(
        [sys.executable, str(script), "next", "412", "--as", "po-user"],
        cwd=ws.home,
        capture_output=True,
        text=True,
        env={**os.environ, "FAB0_GH_FAKE": str(ws.gh_path)},
    )
    out = json.loads(run.stdout)
    assert run.returncode == 1 and out["ok"] is False and str(path) in out["error"]
    assert not (ws.home / ".git" / "fab0" / "pending").exists()


def test_s7_marketplace_and_manifest_agree():
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    entry = next(p for p in market["plugins"] if p["name"] == "fab0")
    assert entry["version"] == VERSION, (
        f"marketplace.json says {entry['version']}, plugins/fab0/.claude-plugin/plugin.json says {VERSION}"
    )
