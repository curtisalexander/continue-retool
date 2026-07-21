# Architecture decision records

These records describe the decisions the current implementation relies on.
They are intentionally short; experiments and superseded alternatives remain in
[`../history/continue-mcp-toolkit-design.md`](../history/continue-mcp-toolkit-design.md).

| ADR | Status | Decision |
|---|---|---|
| [0001](0001-unified-distribution.md) | Accepted | One distribution, selective activation |
| [0002](0002-tool-authority-and-workspace-boundaries.md) | Accepted | Tool authority and workspace boundaries |
| [0003](0003-direct-and-gateway-registration.md) | Accepted | Direct and gateway registration |
| [0004](0004-safe-mutation-and-bounded-execution.md) | Accepted | Safe mutation and bounded execution |

New records are append-only. If a decision changes, add an ADR that supersedes
the old one and update the status and link in this index.
