# fab0-core

The `fab0` plugin marketplace and the core `fab0` plugin for Claude Code.

fab0 runs software delivery as pipelines of Claude Code agents. The pipelines are wired in short YAML files in a project's `.fab0/` directory and carried on GitHub. The project's home repo is [`fab0-dev/fab0`](https://github.com/fab0-dev/fab0).

## Contents

| Path | What |
|---|---|
| `.claude-plugin/marketplace.json` | The `fab0` marketplace |
| `plugins/fab0/bin/fab0-resolve` | The resolver. It validates a project's `.fab0/` wiring, computes the next step of a run, records stage results in the run's ledger, and lists what every open run needs (`inbox`). `fab0-resolve --version` prints the plugin's release as JSON; every stage and gate brief names it in its header. The brief of a per-task stage also has an `## Other tasks of this run` section, right after `## Task`, listing the other items of the step's foreach artifact (id, title, repo, scenarios) in list order; spec and gate briefs don't |
| `plugins/fab0/schemas/` | JSON Schemas for the wiring files, the core artifact types, agent returns and ledger entries |
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
