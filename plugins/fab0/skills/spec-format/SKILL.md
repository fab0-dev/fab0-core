---
name: spec-format
description: The format of a fab0 spec (spec.md) and its task list (tasks.yaml), with the rules a good one follows. Use when writing, revising or reviewing a spec or a task list.
user-invocable: false
---

# Spec and task list format

A work item's spec says **what must be true when the work is done**. Its task list says **how the work splits into pull requests**. Both are reviewed together and approved by merging one pull request. Neither has a status field, because the merge is the approval.

## spec.md

Use these sections, in this order. The headings are fixed, because tools and reviewers find sections by their exact names. A section with nothing to say contains `None`, never nothing: an empty section can't be told apart from a forgotten one.

```markdown
# <Title of the work item>

## Intent

Two to four sentences: who needs what, and why. No solution language.

## Scope

- In: <what this work item delivers>
- Out: <what it deliberately doesn't, even if it looks close>

## Scenarios

### S1: <short name>

- Given <state>
- When <action>
- Then <observable result>

### S2: <short name, a failure case>

- Given …
- When …
- Then …

## Constraints

Security, privacy, performance and compatibility limits the result must respect, or None.

## Decisions

- <decision>: <the choice and the one-line reason>. Implementers don't reopen these.

## Open questions

- <question> — <disposition: answered here | deferred to <where> | asked of <role>>
```

### Rules for the spec

- **Scenarios are the contract.**
  - Each one is concrete and testable, and names an observable result.
  - Ids are `S1`, `S2`, …, unpadded. They are never renumbered: a removed scenario's id isn't reused, and a revised scenario keeps its id.
  - Cover the failure cases. A spec with only happy paths is incomplete.
- **Keep design out of Intent and Scenarios.** No module names, storage, call sequences or signatures. What the implementer must not re-decide goes in Decisions, briefly. Everything else is theirs to decide.
- **Out of scope is explicit.** It is what stops scope creep in implementation and review.
- **Short.** If the scenarios run past about 30, the work item is too big; say so in Open questions instead of writing on.

## tasks.yaml

A YAML list. Each entry is one task: one repository, one pull request.

```yaml
- id: T1
  title: Booking API accepts an extension
  repo: backend
  files: [src/booking/extend.py, tests/booking/test_extend.py]
  traces_to: [S1, S2]
- id: T2
  title: Booking screen offers an extension
  repo: web
  files: [src/screens/booking/, tests/screens/booking/]
  depends_on: [T1]
  traces_to: [S1]
```

| Field | Rule |
|---|---|
| `id` | `T1`, `T2`, …, unpadded, stable across revisions and never reused |
| `title` | What the task delivers, as a short sentence. It becomes the issue and pull request title |
| `repo` | One repository name from the project's `fab0.yaml` |
| `files` | Every file the task creates or changes, tests included. A path ending in `/` means everything under it |
| `depends_on` | Tasks that must be merged before this one can start (optional) |
| `traces_to` | The scenarios this task delivers (optional only for pure groundwork) |

### Rules for tasks

- **Every in-scope scenario is delivered by at least one task.** Check this before replying: list the scenario ids and find each in some task's `traces_to`.
- **One repository per task.** Work that spans repositories is one task per repository, ordered by `depends_on`.
- **Small.** One concern, one reviewable pull request, about half a day of focused work. Split a task that needs more than about six files or ten steps.
- **Vertical over horizontal.** Prefer a task that delivers a scenario end to end within its repository over a task per layer. Use horizontal tasks only for groundwork that several tasks share, such as a migration, shared wiring or a new module skeleton, and put that groundwork first.
- **Disjoint files for parallel work.** Two tasks in the same repository that share a file can't run in parallel; the resolver serialises them. Share files only when the order is real, and then state it with `depends_on`.
- **No cycles.** `depends_on` must name existing tasks and form no loop.
- **Docs travel with code.** If a change makes a README or doc wrong, that file is in the same task's `files`.
