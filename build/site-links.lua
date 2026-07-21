-- Rewrite repository-relative Markdown links for pages rendered into docs/.
-- Source links remain useful on GitHub; published links either target another
-- generated page, a file served under docs/, or the repository source browser.

local source_base = "https://github.com/curtisalexander/continue-retool/blob/main/"

function Link(link)
  local target = link.target

  if target == "ARCHITECTURE.md" or target == "../../ARCHITECTURE.md" then
    link.target = "architecture.html"
  elseif target == "continue-mcp-token-strategy.md"
      or target == "../../continue-mcp-token-strategy.md" then
    link.target = "continue-mcp-token-strategy.html"
  elseif target == "docs/history/continue-mcp-toolkit-design.md" then
    link.target = "continue-mcp-toolkit.html"
  elseif target == "docs/adr/" or target == "../adr/" then
    link.target = "adr/"
  elseif target:match("^docs/adr/") then
    link.target = target:sub(6)
  elseif target:match("^continue%-mcp/") then
    link.target = source_base .. target
  end

  return link
end
