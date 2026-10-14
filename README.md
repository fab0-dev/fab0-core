# fab0-core

The `fab0` plugin marketplace and the core `fab0` plugin for Claude Code.

fab0 is a toolkit for building and operating software factories. A factory is one or more YAML pipelines in a project's `.fab0/` directory: steps run by any agent, command or tool, with a gate on every transition, carried on git and GitHub. The project's home repo is [`fab0-dev/fab0`](https://github.com/fab0-dev/fab0).

> **Refactor in progress.** fab0 is being rebuilt from a delivery factory into a toolkit for building factories. This plugin is frozen at 0.5.0, and no release is made until the refactor's last step. The plan and progress are in [REFACTOR.md](https://github.com/fab0-dev/fab0/blob/main/REFACTOR.md).

## Contents

| Path | What |
|---|---|
| `.claude-plugin/marketplace.json` | The `fab0` marketplace |
| `plugins/fab0/bin/fab0-resolve` | The engine: `validate`, `status`, `next`, `record`, `apply`, `inbox` and `--version`, on schema 2 |
| `plugins/fab0/lib/fab0_engine/` | The engine's modules; `bin/fab0-resolve` is its entry point |
| `plugins/fab0/schemas/` | Schema 2: JSON Schemas for the project file, pipelines, steps, artifacts, verdicts, returns and ledger entries |
| `plugins/fab0/skills/` | `/fab0:run` (executes what the engine hands out), `/fab0:inbox`, and `github` (setup and rules) |
| `tests/` | Fixture-based tests of the engine |
| `scripts/e2e-triage.py` | Runs `/fab0:run` headlessly on the `triage` fixture against a fake tracker (costs tokens) |

## Install

```
/plugin marketplace add fab0-dev/fab0-core
/plugin install fab0@fab0
```

`fab0-resolve` then sits on the Bash `PATH` of every Claude Code session, and `/fab0:run <issue>` and `/fab0:inbox` are available. It needs [`uv`](https://docs.astral.sh/uv/) and an authenticated [`gh`](https://cli.github.com/) with the `project` scope (`gh auth refresh -s project`).

A project is wired by copying an example factory into its `.fab0/` directory, with the agents and skills it uses. Examples live in [`fab0-dev/fab0-examples`](https://github.com/fab0-dev/fab0-examples).

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
claude plugin validate --strict plugins/fab0
claude plugin validate --strict plugins/fab0/skills
```

The end-to-end check runs a real Claude Code session, so run it by hand when a skill, a hand-out or a brief changes:

```bash
uv run python scripts/e2e-triage.py
```

## Licence

[Apache 2.0](LICENSE)
