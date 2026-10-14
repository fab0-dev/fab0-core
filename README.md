# fab0-core

The `fab0` plugin marketplace and the core `fab0` plugin for Claude Code.

fab0 runs software delivery as pipelines of Claude Code agents. The pipelines are wired in short YAML files in a project's `.fab0/` directory and carried on GitHub. The project's home repo is [`fab0-dev/fab0`](https://github.com/fab0-dev/fab0).

> **Refactor in progress.** fab0 is being rebuilt from a delivery factory into a toolkit for building factories. This plugin is frozen at 0.5.0, and no release is made until the refactor's last step. The plan and progress are in [REFACTOR.md](https://github.com/fab0-dev/fab0/blob/main/REFACTOR.md).

## Contents

| Path | What |
|---|---|
| `.claude-plugin/marketplace.json` | The `fab0` marketplace |
| `plugins/fab0/bin/fab0-resolve` | The engine (refactor in progress: only `validate` and `--version` work on schema 2 so far). Below is the 0.5 description: the resolver. It validates a project's `.fab0/` wiring, computes the next step of a run, records stage results in the run's ledger, and lists what every open run needs (`inbox`). `fab0-resolve --version` prints the plugin's release as JSON; every stage and gate brief names it in its header. The brief of a per-task stage also has an `## Other tasks of this run` section, right after `## Task`, listing the other items of the step's foreach artifact (id, title, repo, scenarios) in list order; spec and gate briefs don't. For a stage with an agent gate, a `wait` result with reason `questions`, `human-gate` or `escalated` also carries `review` (`reviewed`, `attempt`, `decision` when reviewed, and the `text` sentence to state) and `draft`: whether the attempt the branch holds has been decided on by the gate, and whether its pull request should be a draft. An attempt that returned questions is never handed to the gate, so it is unreviewed and a draft. A stage without an agent gate has neither field. The resolver only says this; `/fab0:run` applies it |
| `plugins/fab0/lib/fab0_engine/` | The engine's modules; `bin/fab0-resolve` is its entry point |
| `plugins/fab0/schemas/` | Schema 2: JSON Schemas for the project file, pipelines, steps, artifacts, verdicts, returns and ledger entries |
| `plugins/fab0/agents/` | `spec-author` (spec and task list), `implementer` (one task), `reviewer` (the independent gate) |
| `plugins/fab0/skills/` | `/fab0:run` (the orchestrator), `/fab0:inbox`, and two skills agents load: `spec-format` and `github` |
| `tests/` | Fixture-based tests of the resolver |
| `scripts/check-agents.py` | Runs each agent headlessly on a fixture brief and checks its reply with the resolver (costs tokens) |

## Install

```
/plugin marketplace add fab0-dev/fab0-core
/plugin install fab0@fab0
```

`fab0-resolve` then sits on the Bash `PATH` of every Claude Code session, and `/fab0:run <issue>` and `/fab0:inbox` are available. It needs [`uv`](https://docs.astral.sh/uv/) and an authenticated [`gh`](https://cli.github.com/) with the `project` scope (`gh auth refresh -s project`).

A project is wired by copying a recipe into its `.fab0/` directory; recipes live in `fab0-dev/fab0-recipes`.

## Releases

The marketplace on `main` is the catalog of releases. Its `fab0` entry points at the tagged release (`fab0--v<version>`), not at `main`, so unreleased changes on `main` never reach an install. Projects declare the marketplace without a `ref` and with `autoUpdate: true`, so they never change for a release. Claude Code fetches a new release in the background during a session, and loads it at the next start. To fetch it at once:

```bash
claude plugin marketplace update fab0
```

```bash
claude plugin update fab0@fab0 --scope project
```

A release is one PR here:
1. Set the same `version` in `plugins/fab0/.claude-plugin/plugin.json` and in the `fab0` entry of `.claude-plugin/marketplace.json`, and point the entry's `ref` at `fab0--v<version>`.
2. Once it is merged, tag `main` right away, since installs follow the entry as soon as it is on `main`:
   ```bash
   claude plugin tag plugins/fab0 --push
   ```

## Develop

```bash
uv run pytest
uv run ruff check
claude plugin validate --strict .
claude plugin validate --strict plugins/fab0/agents
claude plugin validate --strict plugins/fab0/skills
```

The agent check runs real Claude Code sessions, so run it by hand when an agent or the brief changes:

```bash
uv run python scripts/check-agents.py
```

## Licence

[Apache 2.0](LICENSE)
