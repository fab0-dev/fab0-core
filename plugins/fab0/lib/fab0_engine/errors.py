"""Exit codes, and the exception that stops a command with one."""

from __future__ import annotations

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_INVALID, EXIT_REFUSED = 0, 1, 2, 3, 4


class Fail(Exception):
    """Stops a command with an exit code and a JSON payload."""

    def __init__(self, code: int, error: str, **detail):
        super().__init__(error)
        self.code = code
        self.payload = {"error": error, **detail}
