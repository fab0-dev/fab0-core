"""Test helpers: copies of the fixture factories in tests/fixtures/, and the engine on the command line."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "plugins" / "fab0" / "bin" / "fab0-resolve"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FACTORIES = sorted(p.name for p in FIXTURES.iterdir() if (p / ".fab0").is_dir())

sys.path.insert(0, str(ROOT / "plugins" / "fab0" / "lib"))


class Factory:
    """A copy of one fixture factory, as the home repo of a project."""

    def __init__(self, name: str, tmp: Path):
        self.home = tmp / name
        shutil.copytree(FIXTURES / name, self.home)

    def read(self, rel: str):
        return yaml.safe_load((self.home / ".fab0" / rel).read_text())

    def edit(self, rel: str, change) -> None:
        data = self.read(rel)
        change(data)
        (self.home / ".fab0" / rel).write_text(yaml.safe_dump(data, sort_keys=False))

    def run(self, *args: str, expect: int | None = 0) -> dict:
        result = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.home, capture_output=True, text=True)
        if expect is not None:
            assert result.returncode == expect, result.stdout + result.stderr
        return json.loads(result.stdout)


@pytest.fixture
def factory(tmp_path):
    """factory("story") copies that fixture factory into a temporary directory."""
    return lambda name: Factory(name, tmp_path)
