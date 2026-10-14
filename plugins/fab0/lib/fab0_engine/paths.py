"""Where the plugin's own files are, found relative to this package."""

from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMAS = PLUGIN_ROOT / "schemas"
MANIFEST = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
