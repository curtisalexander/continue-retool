"""Golden tests for sql-mcp. sqruff is a package dependency, so no skips —
if the binary is missing that's a real failure. Run: uv run --extra test pytest -q
"""
import asyncio
import sys

import pytest

from sql_mcp import server


def test_numeric_timeout_is_defaulted_and_clamped(monkeypatch):
    monkeypatch.setenv("SQL_TEST_TIMEOUT", "bad")
    assert server._env_float("SQL_TEST_TIMEOUT", 30.0, 0.1, 300.0) == 30.0
    monkeypatch.setenv("SQL_TEST_TIMEOUT", "-10")
    assert server._env_float("SQL_TEST_TIMEOUT", 30.0, 0.1, 300.0) == 0.1
    monkeypatch.setenv("SQL_TEST_TIMEOUT", "nan")
    assert server._env_float("SQL_TEST_TIMEOUT", 30.0, 0.1, 300.0) == 30.0


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


@pytest.mark.parametrize("tool", [_format, _lint])
def test_spawn_failure_is_structured(monkeypatch, tool):
    monkeypatch.setattr(server, "sqruff_bin", lambda: "/definitely/missing/sqruff")
    res = tool("select 1")
    assert res["ok"] is False
    assert res["error_type"] == "spawn"


def test_decode_failure_is_structured(monkeypatch):
    async def invalid_output(*_args, **_kwargs):
        raise server.SubprocessFailure("decode", "could not decode sqruff output")

    monkeypatch.setattr(server, "_run_sqruff", invalid_output)
    res = _lint("select 1")
    assert res == {
        "ok": False,
        "error": "could not decode sqruff output",
        "error_type": "decode",
    }


def test_invalid_input_encoding_never_spawns(monkeypatch):
    spawned = False

    async def unexpected_spawn(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("invalid input must be rejected before spawning")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_spawn)
    res = _lint("select '\ud800'")
    assert res["ok"] is False
    assert res["error_type"] == "decode"
    assert "encode SQL input" in res["error"]
    assert spawned is False


def test_cancellation_during_communication_kills_and_reaps(monkeypatch):
    processes = []
    communicating = asyncio.Event()
    original_spawn = asyncio.create_subprocess_exec

    async def recording_spawn(*args, **kwargs):
        proc = await original_spawn(*args, **kwargs)
        processes.append(proc)
        communicate = proc.communicate

        async def recording_communication(*args):
            communicating.set()
            return await communicate(*args)

        monkeypatch.setattr(proc, "communicate", recording_communication)
        return proc

    async def scenario():
        monkeypatch.setattr(server, "sqruff_bin", lambda: sys.executable)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_spawn)
        task = asyncio.create_task(
            server._run_sqruff(["-c", "import time; time.sleep(30)"], "select 1")
        )
        await asyncio.wait_for(communicating.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert processes[0].returncode is not None


def test_cancellation_after_child_spawn_but_before_spawn_await_returns(monkeypatch):
    processes = []
    child_created = asyncio.Event()
    original_spawn = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        proc = await original_spawn(*args, **kwargs)
        processes.append(proc)
        child_created.set()
        await asyncio.sleep(0.05)
        return proc

    async def scenario():
        monkeypatch.setattr(server, "sqruff_bin", lambda: sys.executable)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
        task = asyncio.create_task(
            server._run_sqruff(["-c", "import time; time.sleep(30)"], "select 1")
        )
        await child_created.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert processes[0].returncode is not None


def test_timeout_still_kills_and_reaps(monkeypatch):
    processes = []
    original_spawn = asyncio.create_subprocess_exec

    async def recording_spawn(*args, **kwargs):
        proc = await original_spawn(*args, **kwargs)
        processes.append(proc)
        return proc

    async def scenario():
        monkeypatch.setattr(server, "sqruff_bin", lambda: sys.executable)
        monkeypatch.setattr(server, "DEFAULT_TIMEOUT", 0.05)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_spawn)
        with pytest.raises(server.SubprocessFailure, match="timed out") as failure:
            await server._run_sqruff(
                ["-c", "import time; time.sleep(30)"], "select 1"
            )
        assert failure.value.kind == "timeout"

    asyncio.run(scenario())
    assert processes[0].returncode is not None
