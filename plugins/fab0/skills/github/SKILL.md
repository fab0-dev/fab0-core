---
name: github
description: How the fab0 orchestrator writes to GitHub with gh. Covers applying the plan and deliver operations from `fab0-resolve next`, pushing stage branches, opening stage pull requests, posting handoff and question comments, claiming tasks, and updating the Projects board. Only the /fab0:run orchestrator uses it; stage agents never write to GitHub.
user-invocable: false
---

# GitHub writes for the fab0 orchestrator

All of fab0's GitHub writes go through these commands, using the person's own `gh` login. The login needs the `project` scope: `gh auth refresh -s project`.

## Rules

- **Never merge, never push to the base branch, never force-push.** Merging a stage pull request is a person's approval. The project's settings deny these commands too.
- **Write only what `fab0-resolve next` returned.** Never invent an operation, a title or an assignee.
- **Apply operations in the order given.** Each operation is safe to repeat. If one fails, stop: report what was applied and what wasn't, retry nothing, and run `next` again later. The resolver recomputes what is still missing.
- **Pass bodies through files** (`--body-file`, `-F body=@file`), never inline, so quotes and newlines survive.
- **Ids.** `sub_issue_id` and the blocked-by `issue_id` take an issue's numeric `id`, not its `number`. Read the id with:
  ```bash
  gh api repos/OWNER/REPO/issues/NUMBER --jq .id
  ```

## Plan operations

An `action` result with `"action": "plan"` carries `ops`. An issue reference is `{task, repo, number}`. `number` is null for an issue that an earlier `create-issue` in the same list creates; keep the numbers you create, by task id.

| `op` | Command |
|---|---|
| `create-issue` | `gh api -X POST repos/{issue.repo}/issues -f title=… -F body=@body.md -f type={type} -f 'assignees[]=…'` → keep `.number` and `.id` |
| `add-sub-issue` | `gh api -X POST repos/{parent.repo}/issues/{parent.number}/sub_issues -F sub_issue_id={child id}` |
| `add-blocked-by` | `gh api -X POST repos/{issue.repo}/issues/{issue.number}/dependencies/blocked_by -F issue_id={blocked_by id}` |
| `remove-blocked-by` | `gh api -X DELETE repos/{issue.repo}/issues/{issue.number}/dependencies/blocked_by/{blocked_by id}` |
| `edit-title` | `gh issue edit {number} --repo {repo} --title …` |
| `close-issue` | `gh issue close {number} --repo {repo} --reason "not planned"` |

If `create-issue` is refused because the issue type doesn't exist in the organisation, stop and say so. Don't create the issue without its type. Issue types are set up by an organisation owner.

## Deliver operations

An `action` result with `"action": "deliver"` carries `ops` for the home repo. Its local path is in `workspace.home.path`.

| `op` | Command |
|---|---|
| `branch` | `git -C PATH fetch origin && git -C PATH checkout -B {branch} origin/{from}` |
| `append` | append `text` to `PATH/{path}` |
| `remove` | `git -C PATH rm -r -q {path}` |
| `open-pr` | `git -C PATH add -A && git -C PATH commit -m "{title}"`, then `git -C PATH push -u origin {branch}`, then `gh pr create --repo {repo} --base <base> --head {branch} --title … --body-file … --assignee … --reviewer …` |

## Stage branches and pull requests

A `wait` result with reason `human-gate`, `questions` or `escalated` names:
- `repo` (look its path up in `workspace`), `github` and `branch`;
- `pr`: the open pull request, or none yet;
- `reviewers`;
- `closes`: the task issue, for a task;
- `refs`: the run's item.

1. Push the branch: `git -C PATH push -u origin {branch}`.
2. If `pr` is absent, open it:
   ```bash
   gh pr create --repo {github} --base <base> --head {branch} --title "<title>" --body-file body.md --reviewer <reviewers>
   ```
   - Title: `<Stage> #<run>: <item title>`; for a task, `<Stage> <run> <task>: <task title>`.
   - Body: one line saying what the stage produced, its summary from the ledger entry, then `Closes {closes}` for a task or `Refs {refs}` otherwise.
   - On `escalated`, start the body with the gate's reasons.
3. If `pr` exists, the push updates it.
4. **Questions** (reason `questions`): post them as one comment on the pull request, and assign the item to the owner.
   ```bash
   gh pr comment {number} --repo {github} --body-file questions.md
   ```
   The answer comes back as edits or a pull request review. Comments are never read back.
5. **Handoff comment.** Post one on the run's item each time a stage pull request is opened or updated:
   ```bash
   gh issue comment <run> --repo <home github> --body-file handoff.md
   ```
   It gives: the stage (and task), who ran it, the model, the verification results, and the pull request link.

## Claiming a task (`--take`)

When `next --task T` returns `wait` with reason `assigned`, `--take` reassigns the task issue to the person running, then runs `next` again:

```bash
gh issue edit {number} --repo {repo} --add-assignee @me --remove-assignee <previous assignees>
```

## The board

`fab0.yaml` names the project in `tracker: {owner, project, fields: {stage, run, waiting}}`. fab0 writes those three fields, never Status: Projects' built-in workflows own Status.

1. Find the item, adding it if needed:
   ```bash
   gh project item-add {project} --owner {owner} --url <issue url> --format json --jq .id
   ```
2. Find the project, field and option ids:
   ```bash
   gh project view {project} --owner {owner} --format json --jq .id
   ```
   ```bash
   gh project field-list {project} --owner {owner} --format json
   ```
3. Set the fields with `gh project item-edit --project-id … --id <item> --field-id …`:
   - **Stage:** `--single-select-option-id <option named after the stage, or done>`. If the option is missing, report it and skip the field. Don't create options.
   - **Run:** `--text <run id>`.
   - **Waiting on:** `--text "<reason>"`, or `--clear` when nothing is awaited.

Update the board after every recorded stage and at every stop (`wait`, `done`, `blocked`). A board failure is reported, but it never stops a run: the board is a projection.
