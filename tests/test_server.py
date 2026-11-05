"""Testa o servidor pelo protocolo MCP (cliente em processo), não só as funções."""

import json

import pytest
from mcp import Client

from isp_analytics_mcp.server import mcp

EXPECTED_TOOLS = {
    "overview",
    "churn_by_month",
    "churn_reasons",
    "churn_by_segment",
    "churn_risk",
    "delinquency_aging",
    "delinquency_by_month",
    "delinquent_customers",
    "retention_cohorts",
    "sales_by_month",
    "ticket_stats",
    "tickets_by_month",
    "run_sql",
}


pytestmark = pytest.mark.usefixtures("server_env")


async def test_lists_all_tools():
    async with Client(mcp) as client:
        tools = await client.list_tools()
    names = {t.name for t in tools.tools}
    assert names == EXPECTED_TOOLS
    assert all(t.description for t in tools.tools)


async def test_instructions_are_sent():
    async with Client(mcp) as client:
        assert "Aurora Fibra" in (client.instructions or "")


async def test_overview_tool_returns_kpis():
    async with Client(mcp) as client:
        result = await client.call_tool("overview", {"as_of": "2026-11-01"})
    assert not result.is_error
    payload = result.structured_content or json.loads(result.content[0].text)
    assert "active_customers" in payload and "mrr" in payload


async def test_tool_with_literal_argument():
    async with Client(mcp) as client:
        ok = await client.call_tool("churn_by_segment", {"segment": "city"})
        bad = await client.call_tool("churn_by_segment", {"segment": "cpf"})
    assert not ok.is_error
    assert bad.is_error


async def test_run_sql_rejects_write_via_protocol():
    async with Client(mcp) as client:
        result = await client.call_tool("run_sql", {"query": "DELETE FROM customers"})
    assert result.is_error
    assert "SELECT" in result.content[0].text


async def test_schema_resource():
    async with Client(mcp) as client:
        res = await client.read_resource("isp://schema")
    assert "CREATE TABLE IF NOT EXISTS customers" in res.contents[0].text


async def test_monthly_review_prompt():
    async with Client(mcp) as client:
        prompt = await client.get_prompt("monthly_review", {"as_of": "2026-10-01"})
    assert "2026-10-01" in prompt.messages[0].content.text
