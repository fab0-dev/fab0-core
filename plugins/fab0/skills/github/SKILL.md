---
name: github
description: Set up GitHub for a fab0 project, and the rules for GitHub writes. Use it when a project is wired for the first time, when `fab0-resolve apply` fails on GitHub (a missing label, issue type, board field or scope), or before any gh write outside `apply`.
---

# GitHub for fab0

`fab0-resolve apply` makes every GitHub write a run needs: branches, pull requests, merges within the merge policy, labels, assignees, comments, sub-issues, blocked-by relations and board fields. It uses the person's own `gh` login. This skill covers what `apply` can't do: preparing the repositories, the organisation and the board, once.

## Rules

- **Writes for a run go through `apply`.** Don't repeat one of its writes with `gh` by hand. If `apply` fails, show its error, fix the setup below if that is the cause, and run `apply` again: every write checks first, so nothing is done twice.
- **Never merge into the base branch, push to it, or force-push.** Merging a run's pull request into the base is a person's decision, unless the pipeline's `git.auto_merge` allows otherwise.
- **Pass bodies through files** (`--body-file`, `-F body=@file`), never inline.

## Setup

Collect from `.fab0/`: the labels and issue types in pipeline triggers, `tracker.label` actions, `tracker.child.sync` types and `human: {via: label}` gates; the board and field names in `fab0.yaml` `tracker`. Then check each of these, and fix what is missing.

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

Report what was missing, what you created, and what a person (an organisation owner) still has to do.
