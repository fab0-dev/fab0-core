"""An engine older than the plugin Claude Code has installed for the project refuses to run."""

import json
import os
import shutil
import subprocess
import sys

import pytest
from conftest import ROOT, git

PLUGIN = ROOT / "plugins" / "fab0"
CURRENT = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["version"]
COMMANDS = [["validate"], ["status", "412"], ["next", "412"], ["apply", "412"], ["record", "412", "T1"], ["inbox"]]


class Setup:
    def __init__(self, tmp_path, story):
        self.config = tmp_path / "config"
        self.cache = self.config / "plugins" / "cache" / "fab0" / "fab0"
        self.story = story
        self.home = story.home.resolve()
        self.records: dict = {}

    def version_dir(self, version, engine=False):
        path = self.cache / version
        if not path.exists():
            shutil.copytree(PLUGIN, path, ignore=shutil.ignore_patterns("__pycache__"))
            manifest = path / ".claude-plugin" / "plugin.json"
            manifest.write_text(json.dumps({**json.loads(manifest.read_text()), "version": version}))
        return path

    def record(self, version, scope="project", project="home", market="fab0", path=None):
        where = self.home if project == "home" else self.config / "other-project"
        entry = {"scope": scope, "version": version, "installPath": str(path or self.version_dir(version))}
        if scope != "user":
            entry["projectPath"] = str(where)
        self.records.setdefault(f"fab0@{market}", []).append(entry)

    def write(self, text=None):
        record = self.config / "plugins" / "installed_plugins.json"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(text if text is not None else json.dumps({"version": 2, "plugins": self.records}))

    def run(self, engine, *args, cwd=None, env=None):
        script = self.version_dir(engine) / "bin" / "fab0-resolve"
        e = {
            **os.environ,
            "FAB0_GH_FAKE": str(self.story.gh_path),
            "CLAUDE_CONFIG_DIR": str(self.config),
            **(env or {}),
        }
        run = subprocess.run(
            [sys.executable, str(script), *args], cwd=cwd or self.home, capture_output=True, text=True, env=e
        )
        return run.returncode, json.loads(run.stdout)


@pytest.fixture
def setup(tmp_path, story):
    return Setup(tmp_path, story)


def older(setup):
    """Installed 0.6.3 with the cache also holding 0.6.2 (the engine) and 0.6.1."""
    setup.record("0.6.3")
    setup.write()


def test_s1_s3_every_command_refuses(setup):
    older(setup)
    before = git(setup.home, "status", "--porcelain=v1", "--ignored")
    for command in COMMANDS:
        code, out = setup.run("0.6.2", *command)
        assert code == 1 and out["ok"] is False, command
        assert "0.6.2" in out["error"] and "0.6.3" in out["error"]
        assert str(setup.version_dir("0.6.2")) in out["error"] and str(setup.version_dir("0.6.3")) in out["error"]
        assert isinstance(out["fix"], list) and out["fix"]
    assert git(setup.home, "status", "--porcelain=v1", "--ignored") == before
    assert not (setup.home / ".git" / "fab0").exists()


def test_s4_fix_names_orphans_only(setup):
    setup.record("0.6.3")
    setup.record("0.6.1", project="other")
    setup.version_dir("0.6.2")
    setup.write()
    code, out = setup.run("0.6.2", "validate")
    assert code == 1
    text = "\n".join(out["fix"])
    assert str(setup.cache / "0.6.2") in text
    assert "0.6.1" not in text and "0.6.3" not in text
    assert "new Claude Code session" in out["fix"][-1]


def test_s5_nothing_to_remove(setup):
    setup.record("0.6.3")
    setup.record("0.6.2", project="other")
    setup.write()
    code, out = setup.run("0.6.2", "validate")
    assert code == 1
    assert len(out["fix"]) == 1 and "new Claude Code session" in out["fix"][0]


@pytest.mark.parametrize("engine", ["0.6.3", "0.7.0", "0.10.0"])
def test_s6_same_or_newer_runs(setup, engine):
    setup.record("0.6.3")
    setup.write()
    code, out = setup.run(engine, "validate")
    assert code == 0 and "warnings" not in out


def test_s6_ten_is_newer_than_nine(setup):
    setup.record("0.9.0")
    setup.write()
    code, out = setup.run("0.10.0", "validate")
    assert code == 0 and "warnings" not in out


def test_s7_no_record(setup):
    code, out = setup.run("0.6.2", "validate")  # no file at all
    assert code == 0 and "warnings" not in out
    setup.record("0.9.0", project="other")
    setup.record("0.9.0", market="elsewhere", scope="user")
    setup.write()
    code, out = setup.run("0.6.2", "validate")  # none for this project
    assert code == 0 and "warnings" not in out


@pytest.mark.parametrize("bad", ["{not json", "[]"])
def test_s8_unreadable_record(setup, bad):
    setup.write(bad)
    code, out = setup.run("0.6.2", "validate")
    assert code == 0 and "couldn't be checked" in out["warnings"][0]
    code, out = setup.run("0.6.2", "inbox")
    assert code == 0 and "couldn't be checked" in out["warnings"][0]


def test_s8_installed_release_not_a_number(setup):
    setup.record("0.6.3-beta")
    setup.write()
    code, out = setup.run("0.6.2", "validate")
    assert code == 0 and "0.6.3-beta" in out["warnings"][0]


def test_s8_engine_release_not_a_number(setup):
    setup.record("0.6.3")
    setup.write()
    setup.version_dir("0.6.2")
    manifest = setup.version_dir("0.6.2") / ".claude-plugin" / "plugin.json"
    manifest.write_text(json.dumps({"name": "fab0", "version": "dev"}))
    code, out = setup.run("0.6.2", "validate")
    assert code == 0 and "'dev'" in out["warnings"][0]


def test_s9_governing_record(setup, tmp_path):
    setup.record("0.6.1", scope="user")
    setup.record("0.6.3", scope="project")
    setup.record("0.7.0", scope="project", project="other")
    setup.write()
    code, out = setup.run("0.6.2", "validate")
    assert code == 1 and "0.6.3" in out["error"]
    tree = tmp_path / "unit-worktree"
    git(setup.home, "worktree", "add", "-q", "-b", "unit", str(tree))
    code, out = setup.run("0.6.2", "validate", cwd=tree)
    assert code == 1 and "0.6.3" in out["error"]
    setup.record("0.6.2", scope="local")
    setup.write()
    code, out = setup.run("0.6.2", "validate")
    assert code == 0 and "warnings" not in out


def test_s9_user_record_governs_without_project_record(setup):
    setup.record("0.6.3", scope="user")
    setup.write()
    code, out = setup.run("0.6.2", "validate")
    assert code == 1


def test_s10_config_dir(setup, tmp_path):
    older(setup)
    decoy_home = tmp_path / "home"
    (decoy_home / ".claude" / "plugins").mkdir(parents=True)
    (decoy_home / ".claude" / "plugins" / "installed_plugins.json").write_text("{not json")
    code, out = setup.run("0.6.2", "validate", env={"HOME": str(decoy_home)})
    assert code == 1 and "0.6.3" in out["error"]  # read under CLAUDE_CONFIG_DIR, not ~/.claude
    other = tmp_path / "empty-config"
    other.mkdir()
    code, out = setup.run("0.6.2", "validate", env={"CLAUDE_CONFIG_DIR": str(other), "HOME": str(decoy_home)})
    assert code == 0 and "warnings" not in out


def test_s11_version_unchanged(setup):
    older(setup)
    code, out = setup.run("0.6.2", "--version")
    assert code == 0 and out == {"ok": True, "fab0_version": "0.6.2"}
