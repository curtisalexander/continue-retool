# sql-mcp — format and lint SQL with sqruff

Wraps [sqruff](https://github.com/quarylabs/sqruff) — the fast, Rust-built
sqlfluff successor, shipped as prebuilt wheels on PyPI — behind two terse MCP
tools. The same pattern as search-mcp wrapping ripgrep: a proven native binary
does the work; the MCP owns the interface the agent sees.

## Tools

| Tool | What it does |
|---|---|
| `sql.format(sql, dialect?)` | Rewrite SQL to house style; returns `{ok, sql, changed}` |
| `sql.lint(sql, dialect?)` | Report violations as `{code, line, column, message}` |

Both default to the **Snowflake** dialect; pass `dialect` to override per call
(`sqruff dialects` lists the options).

## House style

The packaged config (`sql_mcp/default.sqruff`) enforces:

- **lowercase everything** — keywords, identifiers, functions, literals, types
- **leading commas** — `, column_name` at line start
- sqruff's `core` rule set for the rest (spacing, indentation, line length)

```sql
-- in                                     -- out
SELECT ID, NAME, COUNT(*) AS CNT          select
FROM My_Table GROUP BY ID, NAME;              id
                                              , name
                                              , count(*) as cnt
                                          from my_table
                                          group by id, name
                                          ;
```

To use your own style, point `SQL_MCP_CONFIG` at any `.sqruff` file — the
package config is only the default.

## Setup

```bash
uv run --extra test pytest -q   # sqruff installs as a dependency; no extra step
uv run sql-mcp                  # run the server (stdio)
```

Then register `.continue/mcpServers/sql.yaml` with Continue. This replaces no
built-in tool, so nothing needs excluding; `sql.*` can be **Automatic** (the
tools only transform strings — they never touch disk).
