"""Servidor MCP — expõe as consultas de `queries.py` como tools, o esquema como
resource e um prompt de revisão mensal.

Rodar: `isp-analytics-mcp` (stdio). Para HTTP: `isp-analytics-mcp --http`.
"""

from __future__ import annotations

import argparse
import sqlite3
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import queries as q
from .db import SCHEMA, connect

INSTRUCTIONS = """Servidor de analytics de um provedor de internet fictício (Aurora Fibra).
Os dados são sintéticos e vão até 2026-11-01 (a data "hoje" do dataset).
Comece por `overview` para ter os números do mês. Para investigar uma queda ou
um pico, use `churn_by_month`, `delinquency_by_month` ou `tickets_by_month` e
depois quebre por segmento. `run_sql` aceita apenas SELECT; o esquema está no
resource `isp://schema`. Valores monetários em BRL."""

mcp = MCPServer(
    name="isp-analytics",
    title="ISP Analytics (Aurora Fibra)",
    instructions=INSTRUCTIONS,
    version="0.1.0",
)


def _with_conn(fn, *args: Any, **kwargs: Any) -> Any:
    """Abre a conexão somente leitura, executa e fecha.

    Erros de validação (`ValueError`) e de SQL viram `ToolError`, para que a
    mensagem chegue ao cliente em vez de um "erro inesperado" genérico.
    """
    conn = connect()
    try:
        return fn(conn, *args, **kwargs)
    except (ValueError, sqlite3.Error) as exc:
        raise ToolError(str(exc)) from exc
    finally:
        conn.close()


# ------------------------------------------------------------------ tools


@mcp.tool()
def overview(as_of: str | None = None) -> dict:
    """KPIs do mês: base ativa, MRR, ARPU, churn 30d, inadimplência e chamados abertos.

    Args:
        as_of: data de referência (YYYY-MM-DD). Padrão: 2026-11-01.
    """
    return _with_conn(q.overview, as_of)


@mcp.tool()
def churn_by_month(months: int = 12, as_of: str | None = None) -> list[dict]:
    """Cancelamentos e taxa de churn mês a mês (taxa = cancelados / base ativa no início do mês)."""
    return _with_conn(q.churn_by_month, months, as_of)


@mcp.tool()
def churn_reasons(months: int = 6, as_of: str | None = None) -> list[dict]:
    """Motivos de cancelamento no período (preco, mudanca, concorrente, qualidade, inadimplencia)."""
    return _with_conn(q.churn_reasons, months, as_of)


@mcp.tool()
def churn_by_segment(
    segment: Literal["plan", "city", "channel"] = "plan", as_of: str | None = None
) -> list[dict]:
    """Churn acumulado por plano, cidade ou canal de venda — para achar onde o problema se concentra."""
    return _with_conn(q.churn_by_segment, segment, as_of)


@mcp.tool()
def churn_risk(limit: int = 20, as_of: str | None = None) -> list[dict]:
    """Clientes ativos com maior risco de cancelar, com a pontuação (0–100) e os fatores que a compõem.

    A pontuação é por regras transparentes (atraso, chamados técnicos, tempo de casa, plano),
    não por modelo estatístico. Use para priorizar ações de retenção.
    """
    return _with_conn(q.churn_risk, limit, as_of)


@mcp.tool()
def delinquency_aging(as_of: str | None = None) -> list[dict]:
    """Faturas vencidas e não pagas por faixa de atraso: 01-30, 31-60, 61-90 e 90+ dias."""
    return _with_conn(q.delinquency_aging, as_of)


@mcp.tool()
def delinquency_by_month(months: int = 12, as_of: str | None = None) -> list[dict]:
    """Por mês de vencimento: quantas faturas foram pagas com atraso ou seguem em aberto."""
    return _with_conn(q.delinquency_by_month, months, as_of)


@mcp.tool()
def delinquent_customers(min_days: int = 30, limit: int = 50, as_of: str | None = None) -> list[dict]:
    """Clientes ativos com fatura vencida há pelo menos `min_days` dias, do maior valor para o menor."""
    return _with_conn(q.delinquent_customers, min_days, limit, as_of)


@mcp.tool()
def retention_cohorts(months: int = 12, as_of: str | None = None) -> list[dict]:
    """Coortes por mês de entrada: % de clientes ainda ativos após 1, 3, 6 e 12 meses."""
    return _with_conn(q.retention_cohorts, months, as_of)


@mcp.tool()
def sales_by_month(
    months: int = 6, group_by: Literal["channel", "plan", "city"] = "channel", as_of: str | None = None
) -> list[dict]:
    """Novos clientes e MRR novo por mês, agrupados por canal de venda, plano ou cidade."""
    return _with_conn(q.sales_by_month, months, group_by, as_of)


@mcp.tool()
def ticket_stats(months: int = 3, as_of: str | None = None) -> list[dict]:
    """Chamados por cidade e categoria: volume, % resolvidos e tempo médio de resolução em horas."""
    return _with_conn(q.ticket_stats, months, as_of)


@mcp.tool()
def tickets_by_month(months: int = 12, city: str | None = None, as_of: str | None = None) -> list[dict]:
    """Volume mensal de chamados por categoria, opcionalmente filtrado por cidade. Bom para achar picos."""
    return _with_conn(q.tickets_by_month, months, city, as_of)


@mcp.tool()
def run_sql(query: str, max_rows: int = 200) -> dict:
    """Executa um SELECT livre no banco (somente leitura). Veja o esquema em `isp://schema`.

    Limites: uma instrução por chamada, sem palavras-chave de escrita, no máximo `max_rows` linhas.
    """
    return _with_conn(q.run_sql, query, max_rows)


# ------------------------------------------------------------------ resources


@mcp.resource("isp://schema", mime_type="text/plain")
def schema() -> str:
    """DDL das tabelas: plans, customers, invoices, tickets."""
    return SCHEMA.strip()


@mcp.resource("isp://glossary", mime_type="text/markdown")
def glossary() -> str:
    """Definições das métricas usadas pelas tools."""
    return """# Glossário

- **MRR** — receita mensal recorrente: soma do preço do plano dos clientes ativos.
- **ARPU** — receita média por cliente ativo (MRR / clientes ativos).
- **Churn 30d** — cancelados nos últimos 30 dias / base ativa 30 dias atrás.
- **Churn mensal** — cancelados no mês / base ativa no primeiro dia do mês.
- **Inadimplência** — cliente ativo com ao menos uma fatura vencida e não paga na data de referência.
- **Aging** — faixas de atraso das faturas em aberto (01-30, 31-60, 61-90, 90+ dias).
- **Coorte** — grupo de clientes que entrou no mesmo mês; retenção é a % ainda ativa após N meses.
- **Risco de churn** — pontuação 0–100 por regras: atraso +15/fatura, chamado técnico +10,
  cliente novo +15, plano de entrada +10.
"""


# ------------------------------------------------------------------ prompts


@mcp.prompt()
def monthly_review(as_of: str = "2026-11-01") -> str:
    """Roteiro para uma revisão mensal completa de churn, inadimplência, vendas e suporte."""
    return f"""Faça uma revisão mensal da Aurora Fibra com data de referência {as_of}.

1. Chame `overview` e apresente os KPIs em uma tabela.
2. Chame `churn_by_month` (12 meses) e `churn_reasons` (3 meses); diga se o churn está subindo
   ou caindo e por quê.
3. Chame `churn_by_segment` para plano e para cidade; aponte onde o churn se concentra.
4. Chame `delinquency_aging` e `delinquency_by_month`; destaque sazonalidade e a faixa 90+.
5. Chame `tickets_by_month` (12 meses); se houver um pico, investigue a cidade com
   `tickets_by_month(city=...)`.
6. Chame `retention_cohorts` e comente a retenção em 3 meses das coortes recentes.
7. Feche com 3 a 5 ações recomendadas, cada uma ligada a um número que você viu.

Seja direto. Cite os números. Não invente causas que os dados não mostram."""


# ------------------------------------------------------------------ main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Servidor MCP de analytics da Aurora Fibra.")
    parser.add_argument("--http", action="store_true", help="usa streamable-http em vez de stdio")
    args = parser.parse_args(argv)
    mcp.run(transport="streamable-http" if args.http else "stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
