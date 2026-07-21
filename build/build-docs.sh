#!/usr/bin/env bash
# Render maintained and historical markdown into the GitHub Pages folder.
#
# Usage:  ./build/build-docs.sh        (run from anywhere; resolves its own paths)
#
# Requires pandoc (https://pandoc.org). On macOS: brew install pandoc
#
# The landing page shell is hand-maintained; its server cards are generated.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"
template="$here/doc-template.html"
outdir="$root/docs"

python "$root/continue-mcp/scripts/sync_metadata.py"

if ! command -v pandoc >/dev/null 2>&1; then
  echo "error: pandoc not found. Install it (brew install pandoc) and retry." >&2
  exit 1
fi

# source-markdown  ->  output-html  ::  page title
docs=(
  "ARCHITECTURE.md|architecture.html|continue-mcp — Architecture"
  "continue-mcp-token-strategy.md|continue-mcp-token-strategy.html|Token-Cost Strategy"
  "docs/history/continue-mcp-toolkit-design.md|continue-mcp-toolkit.html|MCP Toolkit — Design History"
)

mkdir -p "$outdir"
for entry in "${docs[@]}"; do
  IFS='|' read -r src out title <<<"$entry"
  echo "rendering $src -> docs/$out"
  pandoc "$root/$src" \
    -f gfm -t html5 -s \
    --toc --toc-depth=2 \
    --lua-filter="$here/site-links.lua" \
    --template="$template" \
    --metadata title="$title" \
    -o "$outdir/$out"
done

echo "done. rebuilt ${#docs[@]} page(s) in docs/"
