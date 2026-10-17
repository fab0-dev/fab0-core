"""End-to-end check: headless Claude Code runs `/fab0:run` on a copy of the `triage` fixture, against a fake
tracker (FAB0_GH_FAKE) and stand-in MCP servers, until the run is done.

The run goes: `apply` labels the report; the `triager` agent classifies it, reading related issues through a
tool; a command gate approves; `apply` labels it by severity and area; the `page` tool step posts to chat and
waits on a person's label. The script adds that label, as the person would, and runs `/fab0:run` again to
finish: `apply` removes the working label and comments.

It runs real sessions, so it costs tokens and needs a logged-in `claude`. Not part of pytest.

    uv run python scripts/e2e-triage.py [--model sonnet] [--budget 3] [--keep]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "fab0"
FIXTURE = ROOT / "tests" / "fixtures" / "triage"
REPORT = {
    "title": "Checkout fails with a saved card",
    "body": "Since this morning, paying with a saved card returns HTTP 500 for every customer. New cards work.",
    "labels": ["needs-triage"],
}


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


class Check:
    def __init__(self, keep: bool, model: str, budget: str):
        self.tmp = Path(tempfile.mkdtemp(prefix="fab0-e2e-"))
        self.keep, self.model, self.budget = keep, model, budget
        self.home = self.tmp / "workspace" / "support"  # the check's own files stay outside the workspace
        self.gh = self.tmp / "gh.json"
        self.log = self.tmp / "mcp.log"
        self.failures: list[str] = []

    def setup(self) -> None:
        shutil.copytree(FIXTURE, self.home)
        git(self.home, "init", "-q", "-b", "main")
        git(self.home, "config", "user.email", "e2e@example.com")
        git(self.home, "config", "user.name", "e2e")
        git(self.home, "add", "-A")
        git(self.home, "commit", "-q", "-m", "start")
        bare = self.tmp / "remotes" / "support.git"
        bare.parent.mkdir()
        git(self.tmp, "init", "-q", "--bare", str(bare))
        git(self.home, "remote", "add", "origin", str(bare))
        git(self.home, "push", "-q", "origin", "main")
        store = self.home.parent / "runs"  # the run store the fixture's fab0.yaml names, with its own origin
        store.mkdir()
        git(store, "init", "-q", "-b", "main")
        git(store, "config", "user.email", "e2e@example.com")
        git(store, "config", "user.name", "e2e")
        self.runs = self.tmp / "remotes" / "runs.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.runs))
        git(store, "remote", "add", "origin", str(self.runs))
        self.gh.write_text(json.dumps({"user": "support-user", "issues": {"acme/support#7": REPORT}}, indent=2))
        server = ROOT / "scripts" / "fake_mcp.py"
        args = {name: [str(server), name, str(self.log)] for name in ("chat", "tracker")}
        servers = {name: {"command": sys.executable, "args": a} for name, a in args.items()}
        (self.tmp / "mcp.json").write_text(json.dumps({"mcpServers": servers}))

    def env(self) -> dict:
        return {**os.environ, "FAB0_GH_FAKE": str(self.gh)}

    def claude(self, prompt: str) -> dict:
        args = [
            "claude",
            "-p",
            prompt,
            "--plugin-dir",
            str(PLUGIN),
            "--mcp-config",
            str(self.tmp / "mcp.json"),
            "--strict-mcp-config",
            "--setting-sources",
            "project,local",
            # the engine under test first on PATH: an installed fab0 plugin's bin would come before --plugin-dir's
            "--settings",
            json.dumps({"env": {"PATH": f"{PLUGIN / 'bin'}{os.pathsep}{os.environ['PATH']}"}}),
            "--model",
            self.model,
            "--dangerously-skip-permissions",
            "--max-budget-usd",
            self.budget,
            "--output-format",
            "json",
        ]
        print(f"$ claude -p {prompt!r} (in {self.home})", flush=True)
        run = subprocess.run(args, cwd=self.home, env=self.env(), capture_output=True, text=True, timeout=3600)
        try:
            out = json.loads(run.stdout)
        except ValueError:
            out = {"result": run.stdout + run.stderr}
        print(out.get("result", ""), flush=True)
        print(f"(cost ${out.get('total_cost_usd', 0):.2f}, {out.get('num_turns', '?')} turns)\n", flush=True)
        return out

    def status(self) -> dict:
        run = subprocess.run(
            [str(PLUGIN / "bin" / "fab0-resolve"), "status", "7"],
            cwd=self.home,
            env=self.env(),
            capture_output=True,
            text=True,
        )
        return json.loads(run.stdout)

    def issue(self) -> dict:
        return json.loads(self.gh.read_text())["issues"]["acme/support#7"]

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def empty_commits(self) -> list[str]:
        """Commits on any branch of the product repository that change nothing (MIGRATION M4)."""
        found = []
        for line in git(self.home, "rev-list", "--all", "--parents").splitlines():
            commit, *parents = line.split()
            if len(parents) == 1 and git(self.home, "rev-parse", f"{commit}^{{tree}}") == git(
                self.home, "rev-parse", f"{parents[0]}^{{tree}}"
            ):
                found.append(git(self.home, "log", "-1", "--format=%s", commit))
        return found

    def expect(self, ok: bool, what: str) -> None:
        print(("  ok    " if ok else "  FAIL  ") + what, flush=True)
        if not ok:
            self.failures.append(what)

    def run(self) -> int:
        self.setup()
        self.claude("/fab0:run 7")
        units = {u["unit"]: u for u in self.status()["units"]}
        labels = self.issue().get("labels", [])
        self.expect(units.get("classify", {}).get("state") == "done", "classify is done")
        self.expect(units.get("label", {}).get("state") == "done", "the severity and area labels are applied")
        self.expect(any(x.startswith("severity-") for x in labels), f"a severity label ({labels})")
        self.expect("needs-triage" not in labels and "triaging" in labels, "onStart swapped the labels")
        self.expect(
            any(c["tool"] == "search_issues" for c in self.calls()), "the agent read related issues through a tool"
        )
        posts = [c for c in self.calls() if c["tool"] == "post_message"]
        self.expect(len(posts) == 1 and posts[0]["arguments"]["channel"] == "on-call", "the page tool posted once")
        self.expect(units.get("page", {}).get("reason") == {"gate": "human"}, "page waits on a person's label")
        ledger = git(self.runs, "ls-tree", "-r", "--name-only", "main", "--", "runs/support/7/ledger")
        self.expect("runs/support/7/ledger/classify-1.yaml" in ledger, "the run store is pushed with the entries")
        product = git(self.home, "log", "--all", "--name-only", "--format=")
        self.expect("ledger" not in product, "no ledger file in the product repository")
        self.expect("runs/support/7/ledger/classify-1-claim.yaml" in ledger, "classify was claimed in the run store")
        self.expect(self.empty_commits() == [], f"no empty commit in the product repository ({self.empty_commits()})")

        data = json.loads(self.gh.read_text())
        data["issues"]["acme/support#7"]["labels"].append("acknowledged")
        self.gh.write_text(json.dumps(data, indent=2))
        self.claude("/fab0:run 7")
        status = self.status()
        issue = self.issue()
        self.expect(status["kind"] == "done", f"the run is done ({status['kind']})")
        self.expect("triaging" not in issue.get("labels", []), "onEnd removed the working label")
        self.expect(any(c["body"].startswith("Triaged as") for c in issue.get("comments", [])), "onEnd commented once")
        self.expect(len([c for c in self.calls() if c["tool"] == "post_message"]) == 1, "nothing was posted twice")

        print(f"\n{len(self.failures)} failed" if self.failures else "\nall passed")
        if self.keep or self.failures:
            print(f"kept in {self.tmp}")
        else:
            shutil.rmtree(self.tmp)
        return 1 if self.failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", default="sonnet", help="the orchestrating session's model")
    parser.add_argument("--budget", default="3", help="the most one session may spend, in USD")
    parser.add_argument("--keep", action="store_true", help="keep the temporary project")
    args = parser.parse_args()
    return Check(args.keep, args.model, args.budget).run()


if __name__ == "__main__":
    sys.exit(main())
