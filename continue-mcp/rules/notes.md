---
name: Consult notes
alwaysApply: true
---

This workspace has persistent agent notes (the `notes.*` MCP tools; stored in
this repo under `.continue-notes/`).

- Before starting a multi-step task, call `notes.list()` and `notes.read` any
  note relevant to the task.
- Before finishing — or when stopping mid-task — record unfinished state,
  discoveries, and corrections with `notes.write`. Make the first line a
  one-line summary; it becomes the hook shown in the index.
- Notes hold facts and state, not policy. If the same preference keeps proving
  true across tasks, propose promoting it to a rule; do not create rules
  unprompted.
- Delete notes that turn out to be wrong (`notes.delete`).
