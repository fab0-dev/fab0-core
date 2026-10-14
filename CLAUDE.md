# fab0-core

The `fab0` marketplace and core plugin. The product's home repo is `fab0-dev/fab0`, a sibling checkout; its `CLAUDE.md` holds the working rules and principles that apply here too.

## Refactor mode

fab0 is being refactored; [REFACTOR.md](https://github.com/fab0-dev/fab0/blob/main/REFACTOR.md) in the home repo is the plan, the tracker and the decision log, and it overrides this file where they disagree. Until its step R8:
- don't release: no version bump, no tag, so auto-update can't hand projects a half-built core;
- don't use `/fab0:run` here; work by hand, one refactor step per PR;
- the engine reads schema 2 only, and every command works;
- the delivery agents, `spec-format` and `tasks.json` left in R4; the `story` example in the examples repo takes them up in R5.

## Layout

- `plugins/fab0/` is what users install. Keep it free of tests and development files.
- `plugins/fab0/bin/fab0-resolve` is the engine's entry point, a thin shim:
  - it runs through `uv run --script`, with PEP 723 inline dependencies (`pyyaml`, `jsonschema`);
  - it lands on the Bash `PATH` when the plugin is enabled;
  - it puts `plugins/fab0/lib/` on `sys.path` and calls `fab0_engine.cli.main`.
- `plugins/fab0/lib/fab0_engine/` is the engine, one module per concern: `wiring` (load and cross-check), `schedule` (units and their states), `handout` (hand-outs, briefs, `record`), `gates` (gate hand-outs and verdicts), `actions` (the read-only check of every catalogue action), `apply` (performing actions, merges and pull requests), `artifacts` (reading, hashing and checking every kind), `hashing`, `ledger`, `claims`, `git`, `tracker` (facts, and the writes `apply` makes), `inbox`, `templates`, `schemas`, `catalogue`, `merge`, `version`, `cli`. It finds the plugin's schemas and manifest relative to itself.
- `plugins/fab0/schemas/` holds the schema 2 JSON Schemas: `project`, `pipeline`, `step`, `artifact`, `verdict`, `return`, `ledger-entry`.
- `plugins/fab0/skills/` holds plain Claude Code skills. `/fab0:run` executes hand-outs and never decides; every word fab0 needs to pass to an agent goes in the brief the engine renders, so any agent can be a step (P1).
- Core ships no agents (P0). Agents belong to factories; the examples repo has them.
- `tests/` drive the engine against copies of the fixture factories in `tests/fixtures/` (`story`, `docs`, `triage`), through the shim, and import `fab0_engine` modules directly for unit tests.

## Conventions

- Python 3.12+, standard library plus `pyyaml` and `jsonschema`. No other runtime dependency.
- The engine is deterministic. Only `apply` writes to git refs and the tracker (D5 in REFACTOR.md); `record` writes only ledger files in a unit's checkout, and `next` only creates a unit's branch when it claims the unit. `apply` changes branches through plumbing, or in their checkout only when it is clean, so nobody's work moves.
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
claude plugin validate --strict plugins/fab0
claude plugin validate --strict plugins/fab0/skills
```

After changing a skill, a hand-out or a brief, run `uv run python scripts/e2e-triage.py`. It costs tokens.

Before a PR, check that no noun from the predecessor project or from a consuming project appears in the diff, its commit messages or the PR text. Keep the list of those nouns local; never commit it.
