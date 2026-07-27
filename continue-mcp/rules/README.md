# rules — workspace rules that make the toolkit work

Continue rules are static policy injected into the system prompt. This directory
retains one optional example; the installer does not copy it automatically.

| Rule | What it does |
|---|---|
| `rule-rule.md` | Optional guidance for concise, durable Continue rules. The installer does not copy it automatically. |

Rules survive context resets but agents should not rewrite them implicitly.
Keep durable project policy concise, reviewed, and version-controlled.
