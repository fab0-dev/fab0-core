# fab0-core

The `fab0` plugin marketplace and the core `fab0` plugin for Claude Code.

fab0 runs software delivery as pipelines of Claude Code agents. The pipelines are wired in short YAML files in a project's `.fab0/` directory and carried on GitHub. The project's home repo is [`fab0-dev/fab0`](https://github.com/fab0-dev/fab0).

## Contents

| Path | What |
|---|---|
| `.claude-plugin/marketplace.json` | The `fab0` marketplace |
| `plugins/fab0/bin/fab0-resolve` | The resolver. It validates a project's `.fab0/` wiring, computes the next stage of a run, and records stage results in the run's ledger |
| `plugins/fab0/schemas/` | JSON Schemas for the wiring files, the core artifact types, agent returns and ledger entries |
| `tests/` | Fixture-based tests of the resolver |

The core agents and the `/fab0:run` and `/fab0:inbox` skills come in the next step.

## Install

```
/plugin marketplace add fab0-dev/fab0-core
/plugin install fab0@fab0
```

`fab0-resolve` then sits on the Bash `PATH` of every Claude Code session. It needs [`uv`](https://docs.astral.sh/uv/) and an authenticated [`gh`](https://cli.github.com/).

## Develop

```bash
uv run pytest
uv run ruff check
claude plugin validate .
```

## Licence

[Apache 2.0](LICENSE)
