# fab0-core

The `fab0` marketplace and core plugin. The product's home repo is `fab0-dev/fab0`, a sibling checkout; its `CLAUDE.md` holds the working rules and principles that apply here too.

## Layout

- `plugins/fab0/` is what users install. Keep it free of tests and development files.
- `plugins/fab0/bin/fab0-resolve` is one self-contained Python script:
  - it runs through `uv run --script`, with PEP 723 inline dependencies (`pyyaml`, `jsonschema`);
  - it lands on the Bash `PATH` when the plugin is enabled;
  - it finds its schemas relative to itself.
- `plugins/fab0/agents/` and `plugins/fab0/skills/` are plain Claude Code agents and skills:
  - an agent describes its role and its output, and never names stages, pipelines or the agent that comes next;
  - every word fab0 needs to pass to an agent goes in the brief the resolver renders.
- `tests/` drive the script against temporary git repos made from `tests/fixtures/`, with a JSON-file fake of GitHub (`FAB0_GH_FAKE`).

## Conventions

- Python 3.12+, standard library plus `pyyaml` and `jsonschema`. No other runtime dependency.
- The resolver is deterministic and never writes to GitHub. It reads GitHub with `gh`, and writes only ledger entries and its pending snapshots under `.git/fab0/`.
- Every command prints one JSON object. Exit codes: 0 ok, 1 error, 2 usage, 3 invalid wiring, 4 refused.
- Ids and ledger names are never zero-padded.
- Schemas are JSON Schema 2020-12.

## Releases

The `fab0` entry of `.claude-plugin/marketplace.json` points at a release tag, never at `main`; projects follow it with auto-update and pin nothing. A release is one PR: the same `version` in `plugin.json` and the marketplace entry, and the entry's `ref` set to `fab0--v<version>`. Tag `main` with `claude plugin tag plugins/fab0 --push` as soon as it is merged. The README has the steps.

## Checks

```bash
uv run pytest
uv run ruff check
claude plugin validate --strict .
claude plugin validate --strict plugins/fab0/agents
claude plugin validate --strict plugins/fab0/skills
```

After changing an agent, a skill an agent loads, or the brief, run `uv run python scripts/check-agents.py`. It costs tokens.

Before a PR, check that no noun from the predecessor project or from a consuming project appears in the diff, its commit messages or the PR text. Keep the list of those nouns local; never commit it.
