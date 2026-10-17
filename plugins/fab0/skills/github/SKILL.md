---
name: github
description: Set up GitHub for a fab0 project, and the rules for GitHub writes. Use it when a project is wired for the first time, when `fab0-resolve apply` fails on GitHub (a missing label, issue type, board field or scope), when `fab0-resolve validate` can't reach the runs repository, or before any gh write outside `apply`.
---

# GitHub for fab0

`fab0-resolve apply` makes every GitHub write a run needs: branches, pull requests, merges within the merge policy, labels, assignees, comments, sub-issues, blocked-by relations and board fields. It uses the person's own `gh` login. This skill covers what `apply` can't do: preparing the repositories, the runs repository, the organisation and the board, once.

## Rules

- **Writes for a run go through `apply`.** Don't repeat one of its writes with `gh` by hand. If `apply` fails, show its error, fix the setup below if that is the cause, and run `apply` again: every write checks first, so nothing is done twice.
- **Never merge into the base branch, push to it, or force-push.** Merging a run's pull request into the base is a person's decision, unless the pipeline's `git.auto_merge` allows otherwise.
- **Pass bodies through files** (`--body-file`, `-F body=@file`), never inline.

## Setup

Collect from `.fab0/`: the labels and issue types in pipeline triggers, `tracker.label` actions, `tracker.child.sync` types and `human: {via: label}` gates; the board and field names in `fab0.yaml` `tracker`; the runs repository in `fab0.yaml` `store.github`, and the logins in `people`. Then check each of these, and fix what is missing.

1. **Login.** `gh auth status` shows the scopes. A board needs `project`: `gh auth refresh -s project`.
2. **Labels**, in the home repo:
   ```bash
   gh label list --repo OWNER/REPO --limit 200 --json name --jq '.[].name'
   ```
   ```bash
   gh label create NAME --repo OWNER/REPO --description "fab0"
   ```
   Labels a pipeline adds from data, such as `severity-{classify.severity}`, can't be listed ahead; create the ones the factory documents.
3. **Issue types.** Triggers and `tracker.child.sync` name issue types, which belong to the organisation. Only an organisation owner creates them, under the organisation's settings. If one is missing, say so and stop; don't create items without their type.
4. **The board**, if `tracker.project` is set:
   ```bash
   gh project field-list PROJECT --owner OWNER --format json
   ```
   - Every field named in `tracker.fields` must exist. A single-select field needs an option for every value an action sets; `apply` doesn't create options.
   - `tracker.fields.signal`, if set, must be a text field: `apply` writes where each item is into it.
   - fab0 never writes Status: the board's built-in workflows own it.
5. **The base branch.** Protect it with a ruleset that requires a pull request, so a run reaches it only through its run pull request. Run branches (`fab0/<run>/base`) stay unprotected, so `apply` can merge units into them.
6. **Sub-issues and blocked-by relations** need nothing beyond the repository's issues being on. The engine reads them on every call and treats them as dependencies.
7. **The runs repository** (`store.github`) holds what runs produce for themselves: each run's ledger, its store artifacts, and the review pull requests of units whose outputs are all in the store. The engine pushes to its `main` directly, with each person's own login, and never force-pushes. Several factories of one product share it, each under its own `store.project`, so it may exist already.
   - **Check** that it exists, its visibility, that its ruleset is in place, and who can write:
     ```bash
     gh repo view OWNER/RUNS --json visibility,defaultBranchRef
     ```
     ```bash
     gh api repos/OWNER/RUNS/rulesets --jq '.[].name'
     ```
     ```bash
     gh api repos/OWNER/RUNS/collaborators --jq '.[] | select(.permissions.push) | .login'
     ```
   - **Create it only when the person asks.** Say first what you will create, then:
     ```bash
     gh repo create OWNER/RUNS --private --description "fab0 run store"
     ```
     Private keeps the runs' specs and records to the project's people, but GitHub allows a ruleset on a private repository only on a paid plan. On a free plan, ask the person: a public repository with the ruleset (the right choice when the product repositories are public already, since the runs hold nothing more), or a private one without it, which leaves `main` open to a person's force push.
     An empty repository is fine: the first record starts `main`.
   - **The ruleset** blocks force pushes to `main` and its deletion, so the store's history stays append-only. It requires no pull request and no checks, since the engine writes `main` directly. Write it to a file, then:
     ```json
     {"name": "fab0 run store", "target": "branch", "enforcement": "active",
      "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
      "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}]}
     ```
     ```bash
     gh api repos/OWNER/RUNS/rulesets -X POST --input ruleset.json
     ```
   - **Write access** for everyone in `people`, who records runs and merges review pull requests: a team the organisation already has, or each login.
     ```bash
     gh api -X PUT repos/OWNER/RUNS/collaborators/LOGIN -f permission=push
     ```
   - Then `fab0-resolve validate`: it fails when the runs repository can't be reached.

Report what was missing, what you created, and what a person (an organisation owner) still has to do.
