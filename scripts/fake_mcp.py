"""A stand-in MCP server over stdio, for the end-to-end check: it answers the tools the `triage` fixture
names and appends every call to a log file.

    python3 fake_mcp.py chat|tracker LOG
"""

import json
import sys

TOOLS = {
    "chat": [
        {
            "name": "post_message",
            "description": "Post a message to a chat channel.",
            "inputSchema": {
                "type": "object",
                "properties": {"channel": {"type": "string"}, "text": {"type": "string"}},
                "required": ["channel", "text"],
            },
        }
    ],
    "tracker": [
        {
            "name": "search_issues",
            "description": "Search the issue tracker.",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "state": {"type": "string"}},
                "required": ["query"],
            },
        }
    ],
}
ANSWERS = {
    "post_message": "Posted to the channel as message 1.",
    "search_issues": json.dumps([{"number": 3, "title": "Card payments time out", "state": "open"}]),
}


def main() -> None:
    server, log = sys.argv[1], sys.argv[2]
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue  # a notification
        method = msg.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": server, "version": "0"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS[server]}
        elif method == "tools/call":
            name = msg["params"]["name"]
            call = {"server": server, "tool": name, "arguments": msg["params"].get("arguments")}
            with open(log, "a") as f:
                f.write(json.dumps(call) + "\n")
            result = {"content": [{"type": "text", "text": ANSWERS[name]}]}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": method}}))
            sys.stdout.flush()
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
