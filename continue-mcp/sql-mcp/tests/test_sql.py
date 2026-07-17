"""Golden tests for sql-mcp. sqruff is a package dependency, so no skips —
if the binary is missing that's a real failure. Run: uv run --extra test pytest -q
"""
import asyncio

from sql_mcp import server


def _format(sql: str, **kw) -> dict:
    return asyncio.run(server.format(sql, **kw)).structured_content


def _lint(sql: str, **kw) -> dict:
    return asyncio.run(server.lint(sql, **kw)).structured_content


def test_format_lowercases_everything():
    res = _format("SELECT ID, NAME FROM My_Table WHERE X IS NULL;")
    assert res["ok"] is True and res["changed"] is True
    out = res["sql"]
    assert "select" in out and "SELECT" not in out
    assert "my_table" in out
    assert "is null" in out


def test_format_leading_commas():
    res = _format(
        "SELECT customer_identifier_number, customer_full_legal_name, "
        "customer_email_address_primary, total_lifetime_order_value "
        "FROM analytics.customer_summary;"
    )
    assert res["ok"] is True
    lines = [ln.strip() for ln in res["sql"].splitlines()]
    # multi-line select list breaks with commas at line start
    assert any(ln.startswith(", ") or ln.startswith(",") for ln in lines)


def test_format_clean_sql_unchanged():
    clean = "select a\nfrom b\n;\n"
    res = _format(clean)
    assert res["ok"] is True
    assert res["changed"] is False
    assert res["sql"] == clean


def test_format_snowflake_syntax_accepted():
    # qualify is Snowflake-specific; the default dialect must accept it
    res = _format(
        "select id, row_number() over (partition by id order by ts) as rn "
        "from t qualify rn = 1;"
    )
    assert res["ok"] is True
    assert "qualify" in res["sql"]


def test_lint_reports_codes_and_positions():
    res = _lint("SELECT A FROM b;")
    assert res["ok"] is True
    assert res["count"] > 0
    codes = {v["code"] for v in res["violations"]}
    assert "CP01" in codes  # keyword capitalisation
    v = res["violations"][0]
    assert v["line"] == 1 and isinstance(v["column"], int) and v["message"]


def test_lint_clean_sql_is_empty():
    res = _lint("select a\nfrom b\n;\n")
    assert res["ok"] is True
    assert res["count"] == 0
    assert res["violations"] == []


def test_dialect_override():
    # generate_series is fine in postgres; the override must reach sqruff
    res = _lint("select * from generate_series(1, 10);", dialect="postgres")
    assert res["ok"] is True


def test_lint_sqruff_failure_is_not_clean(monkeypatch):
    """When sqruff dies without a report (bad config, crashed binary), lint must
    surface an error — never 'clean — 0 violations'."""
    monkeypatch.setenv("SQL_MCP_CONFIG", "/nonexistent/.sqruff")
    res = _lint("select 1")
    assert res["ok"] is False
    assert "violations" not in res


def test_format_sqruff_failure_is_an_error(monkeypatch):
    monkeypatch.setenv("SQL_MCP_CONFIG", "/nonexistent/.sqruff")
    res = _format("select 1")
    assert res["ok"] is False


def test_format_empty_input_is_a_noop_not_a_false_error(tmp_path):
    """Empty / whitespace-only SQL used to trip the no-output path and report a
    misleading 'unparsable SQL' error. sqruff treats it as a valid no-op; so do we."""
    for blank in ("", "   ", "\n\t \n"):
        res = _format(blank)
        assert res["ok"] is True, f"{blank!r} should be ok"
        assert res["changed"] is False
        assert res["sql"] == blank
