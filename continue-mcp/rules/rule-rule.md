---
name: Authoring rules
description: How to write Continue rules for this workspace (the rule rule)
---

When creating or editing a Continue rule (including via the built-in Create
Rule Block tool):

- **One concern per rule, ≤ ~80 tokens of body.** Every always-on rule is paid
  for on every request — the same token discipline as tool descriptions.
- **Scope it.** Use `globs`/`description` so the rule loads only when relevant;
  reserve `alwaysApply: true` for true universals.
- **Rules are policy, not memory.** "How we do things" belongs in a rule;
  facts, task state, and discoveries belong in notes (`notes.write`).
- **Propose, don't impose.** A preference must prove itself repeatedly (see
  the notes rule) before becoming a rule — suggest it to the user rather than
  silently creating it.
