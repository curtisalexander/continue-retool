# rules — workspace rules that make the toolkit work

Continue rules are static policy injected into the system prompt. Two ship
here; copy them into your workspace's `.continue/rules/` directory.

| Rule | What it does |
|---|---|
| `notes.md` | The discovery half of notes-mcp: tells the agent to consult `notes.list()` at task start and record state at task end. Without this rule, notes exist but nothing triggers the agent to check them. |
| `rule-rule.md` | The meta-rule: token discipline and scoping for any rule the agent (or you) authors, and the notes→rule promotion etiquette. |

The division of labor: **rules are policy, notes are memory.** A rule survives
a context reset but the agent can't update it mid-task; a note survives the
reset *and* the agent authors it. Durable preferences start as notes and get
promoted into rules by you, deliberately — that pipeline is exactly what these
two rules encode.
