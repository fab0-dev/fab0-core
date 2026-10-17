"""Fixture check: the classification gate passes when the attempt it judges, at `{attempt}`, has the return's data."""

import sys
from pathlib import Path

sys.exit(0 if "\ndata:" in Path(sys.argv[1]).read_text() else 1)
