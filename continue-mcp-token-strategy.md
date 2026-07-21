# Keeping Token Costs Down: Which Tools to Replace, and Where to Put Them

*Strategy record, begun 2026-07-01. Companion to `ARCHITECTURE.md`. Answers:
how do I replace Continue's tools to minimize token cost — and for each tool, do I
register it **directly** (paid every request) or hide it behind the **gateway**
(paid per use)? The long-tail-vs-starting-cost tradeoff, made concrete.*

---

## 0. TL;DR — the recommendation

| Move | When | Why |
|---|---|---|
| **Exclude built-ins you never use** | Always, first | Free win. Every built-in tool you don't need is pure resting tax you can delete in the tool-policy UI. |
| **Replace hot tools with *terse* MCPs, keep them DIRECT** | edit, shell-run, grep — the 2–3 you use every message | Cuts resting cost (terse < fat built-in) *without* adding latency. This alone fixes most of the pain. |
| **Gateway the long tail** | Once you have ~10+ occasional tools | Near-zero resting cost for tools you rarely touch; a small per-use hop you barely notice because you barely use them. |
| **Enforce terse descriptions** | Every new tool | ≤ 2 sentences, ≤ ~80 tokens — baked into the `new-mcp-tool` factory. |

**The one mental model to keep:** a tool's *gateway-worthiness* ≈
**`schema_size × (1 − how_often_you_use_it)`**. Big schema **and** rarely used →
hide it behind the gateway. Small **or** used constantly → keep it direct. Savings
and cost pull in opposite directions with usage — that's the whole trick (§3).

---

## 1. Two kinds of token cost

There are exactly two ways tools cost you tokens, and they behave completely
differently:

| | **Resting cost** (direct tools) | **Per-use cost** (gateway tools) |
|---|---|---|
| What | Every registered tool's schema sits in context **on every request** | The `search`→`describe`→`call` round-trips, paid **only when you use the tool** |
| Occupies the window? | **Yes, always** — even on turns you don't use the tool | Only on turns you use it (via `describe`) |
| Scales with | number of tools × requests in the session | how *often* you actually invoke the tool |
| Hurts | window room (crowds out code) + $ on cache misses | latency (extra turns) + a little reasoning burden |
| Prompt caching helps? | **Yes** — a stable schema block caches, so $ drops after turn 1 (but it still occupies the window) | N/A — these are dynamic, mid-context |

Two things fall out of this table immediately:

1. **Your pain determines your lever.** If the pain is **$**, prompt caching
   already makes direct tools cheap after the first turn — so replacing fat
   built-ins with terse direct MCPs may be all you need. If the pain is **window
   room / quality degradation** (context half-eaten before you type), the gateway
   is what buys the window back, because caching doesn't free window space.
2. **Resting cost is paid whether or not you use the tool.** That's why 40 rarely-
   used tools registered directly is the worst case — you pay for all of them, all
   the time, to use a few of them occasionally.

---

## 2. The ladder of token levers (cheapest → most involved)

Climb only as far as your pain requires. Most people are fixed by rungs 0–2.

**Rung 0 — Exclude what you don't use.** Continue's tool policies let you set any
built-in to **Excluded**. Every excluded tool's schema leaves the prompt. This is
free and immediate; do it before anything clever.

**Rung 1 — Replace fat built-ins with terse direct MCPs.** The built-in tool
prompts are verbose. A terse MCP equivalent (name + ≤ 2-sentence description +
minimal schema, ~150–300 tokens) replacing a fat built-in (~800+ tokens), kept
**directly registered**, cuts resting cost with **zero latency cost**. This is the
sweet spot for your everyday tools — you already built `edit`, `search`, and
`shell` this way.

**Rung 2 — Gateway the long tail.** For the many tools you use *occasionally*,
register only the gateway and let it hide their schemas until needed (§3). Resting
cost for the whole tail collapses to the gateway's fixed ~3-schema footprint.

**Rung 3 — Enforce terseness at creation.** The `new-mcp-tool` factory bakes in a
token budget per description and registers new tools into the gateway catalog, so
the toolkit can grow without the prompt growing. Discipline by construction, not
willpower.

---

## 3. The head/tail decision, made concrete

Here's the math that makes the recommendation non-arbitrary. Over a session of `R`
requests, consider one tool with schema size `S`, used on a fraction `f` of turns:

```text
  DIRECT   : schema present on ALL R turns          window-occupancy ≈ S · R
  GATEWAY  : schema pulled (via describe) only on    window-occupancy ≈ S · f · R
             the f·R turns you actually use it       (+ tiny search overhead)

  GATEWAY SAVINGS  ≈ S · R · (1 − f)      → grows as the tool is used LESS
  GATEWAY COST     ≈ latency + reasoning  → grows as the tool is used MORE
                     on the f·R uses
```

**Savings scale with `(1 − f)`; cost scales with `f`. They move in opposite
directions.** That single fact decides everything:

- **Rarely-used tool (`f → 0`):** you save almost its entire resting cost, and pay
  the hop almost never. **Gateway wins big.**
- **Constantly-used tool (`f → 1`):** the gateway saves you **almost nothing** —
  because `describe` drags its schema into context on nearly every turn *anyway* —
  while you pay the discovery hop and reasoning overhead every time. **Keep it
  direct.**

That second point is the one people miss: **hiding a hot tool behind the gateway
doesn't actually save window tokens**, because you keep loading its schema to use
it. You get the cost with none of the benefit.

### The 2×2

```text
                        used OFTEN               used RARELY
                ┌─────────────────────────┬─────────────────────────┐
  BIG schema    │  DIRECT                 │  GATEWAY  *             │
                │  (worth its rent;       │  (biggest resting tax   │
                │   gateway saves ~0)     │   for least benefit)    │
                ├─────────────────────────┼─────────────────────────┤
  SMALL schema  │  DIRECT                 │  either — lean GATEWAY  │
                │  (cheap; latency        │  if it's part of a      │
                │   matters more)         │   large tail            │
                └─────────────────────────┴─────────────────────────┘
```

The clean gateway win is the **top-right: big schema, rarely used.** The clean
direct win is the **whole left column: anything you use often**, regardless of
size, because latency and the near-zero savings both argue for keeping it direct.

### Don't forget the gateway's own fixed cost

<!-- BEGIN GENERATED GATEWAY COST -->
The audit currently estimates the gateway's three meta-tool schemas at **~311 tokens at rest**. The default tail (sql, notes) would cost ~574 tokens if registered directly, so the gateway saves ~263 resting tokens (46%) before any per-use describe result. These values are generated from `continue-mcp/bench/schema-metrics.json` using the repository's deterministic `ceil(serialized characters / 4)` estimator.
<!-- END GENERATED GATEWAY COST -->

---

## 4. Worked example — the current toolkit

<!-- BEGIN GENERATED TOOLKIT EXAMPLE -->
The current measured inventory is:

| Server | Tools | Schema ~tokens | Default registration |
|---|---:|---:|---|
| `hello-mcp` | 3 | 187 | direct |
| `shell-mcp` | 7 | 937 | direct |
| `search-mcp` | 2 | 357 | direct |
| `edit-mcp` | 5 | 559 | direct |
| `fs-mcp` | 2 | 255 | direct |
| `sql-mcp` | 2 | 185 | gateway |
| `notes-mcp` | 5 | 389 | gateway |
| `gateway-mcp` | 3 | 311 | gateway-host |

With the metadata defaults, direct servers cost ~2295 tokens and the gateway costs ~311, for **~2606 resting tokens**. Registering the same tail directly would cost ~2869; the generated hybrid saves ~263 tokens per request.

Concretely for Continue, register `hello.yaml`, `shell.yaml`, `search.yaml`, `edit.yaml`, `fs.yaml`, and `gateway.yaml`; keep only `sql`, `notes` in the gateway downstream configuration. Do not register a downstream both directly and through the gateway.
<!-- END GENERATED TOOLKIT EXAMPLE -->

---

## 5. Caveats and second-order effects

- **Prompt caching changes the $ picture, not the window picture.** A stable block
  of directly-registered tool schemas caches well, so after the first turn the *$*
  cost of resting tools is small. But those schemas still *occupy the window*, and
  that's usually the real complaint (context crowded out, quality degrades). The
  gateway is a window play first, a $ play second.
- **Keep the tool block stable to preserve the cache.** Tool schemas usually sit at
  the top of the prompt (a cache prefix). Adding/removing directly-registered tools
  mid-session busts that cache. The gateway helps here too: you change
  `gateway.config.json`, not Continue's registered tool set, so the cached prefix
  stays stable.
- **Reliability has a token cost too.** A model that fumbles the gateway's
  search→describe→call dance burns tokens on retries. Terse, well-named tools and
  good summaries (what `search` returns) keep navigation cheap. This is another
  reason to keep the *hot* path direct — you don't want the tool you use constantly
  to depend on a 3-step negotiation.
- **"Terse" is not "cryptic."** Cutting descriptions saves tokens only up to the
  point the model starts guessing wrong and retrying. Budget ~80 tokens, spend them
  on the name and the one distinguishing sentence.

---

## 6. Recommendation, in one paragraph

Exclude the built-ins you don't use (free). Replace the built-ins you *do* use with
**terse, directly-registered** MCPs — that fixes most of the bloat with no latency
cost, and prompt caching keeps their $ low. Only once you've accumulated a **long
tail (~8–10+) of occasional tools** should you stand up the gateway, and then put
**only the tail** behind it while keeping your 2–3 everyday tools direct. Decide
each tool by `schema_size × (1 − usage)`: **big and rare → gateway; small or
constant → direct.** Grow the tail through the factory so new tools land behind the
gateway with terse descriptions and zero added resting cost. That gives you low
resting tokens, a fast hot path, and unlimited expansion — the three goals at once.

---

## Sources

- [MCP context-bloat fix 2026: Tool Search / Code Mode / progressive disclosure](https://mcp.directory/blog/mcp-context-bloat-fix-2026-tool-search-code-mode-progressive-disclosure)
- [MCP token optimization compared (StackOne)](https://www.stackone.com/blog/mcp-token-optimization/) · [Your MCP server is eating your context window (Apideck)](https://www.apideck.com/blog/mcp-server-eating-context-window-cli-alternative)
- [Anthropic — Tool Search Tool & code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp) · [How Agent Mode Works — Continue Docs](https://docs.continue.dev/ide-extensions/agent/how-it-works)
- Companion docs in this repo: `ARCHITECTURE.md` (current topology),
  `docs/history/continue-mcp-toolkit-design.md` (§5 token tax, §5b gateway), and
  `continue-mcp/gateway-mcp/README.md` (head/tail operation)
</content>
