"""Consultas analíticas — funções puras sobre uma conexão SQLite.

Separadas do servidor MCP de propósito: são testáveis sem protocolo, e o
servidor fica sendo só uma camada fina de exposição.

Todas recebem `as_of` (data de referência, ISO) para que as respostas sejam
reprodutíveis; o padrão é a data "hoje" do dataset sintético.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from typing import Any

DEFAULT_AS_OF = "2026-11-01"

Row = dict[str, Any]


def _rows(cur: sqlite3.Cursor) -> list[Row]:
    return [dict(r) for r in cur.fetchall()]


def _as_of(value: str | None) -> date:
    return date.fromisoformat(value or DEFAULT_AS_OF)


def _month_start(d: date, months_back: int) -> str:
    y, m = divmod(d.month - 1 - months_back, 12)
    return date(d.year + y, m + 1, 1).isoformat()


# --------------------------------------------------------------------------- #
# Visão geral
# --------------------------------------------------------------------------- #


def overview(conn: sqlite3.Connection, as_of: str | None = None) -> Row:
    """KPIs do mês: base ativa, MRR, churn dos últimos 30 dias, inadimplência e chamados abertos."""
    ref = _as_of(as_of)
    d30 = (ref - timedelta(days=30)).isoformat()
    active = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(p.price), 0) mrr FROM customers c "
        "JOIN plans p ON p.id = c.plan_id WHERE c.status = 'active'"
    ).fetchone()
    cancelled_30d = conn.execute(
        "SELECT COUNT(*) FROM customers WHERE status = 'cancelled' AND cancel_date > ? AND cancel_date <= ?",
        (d30, ref.isoformat()),
    ).fetchone()[0]
    base_30d_ago = active["n"] + cancelled_30d
    overdue = conn.execute(
        "SELECT COUNT(DISTINCT customer_id) customers, COALESCE(SUM(amount), 0) amount "
        "FROM invoices WHERE paid_date IS NULL AND due_date < ?",
        (ref.isoformat(),),
    ).fetchone()
    open_tickets = conn.execute("SELECT COUNT(*) FROM tickets WHERE closed_at IS NULL").fetchone()[0]
    return {
        "as_of": ref.isoformat(),
        "active_customers": active["n"],
        "mrr": round(active["mrr"], 2),
        "arpu": round(active["mrr"] / active["n"], 2) if active["n"] else 0.0,
        "cancelled_last_30d": cancelled_30d,
        "churn_rate_30d_pct": round(100 * cancelled_30d / base_30d_ago, 2) if base_30d_ago else 0.0,
        "overdue_customers": overdue["customers"],
        "overdue_amount": round(overdue["amount"], 2),
        "overdue_rate_pct": round(100 * overdue["customers"] / active["n"], 2) if active["n"] else 0.0,
        "open_tickets": open_tickets,
    }


# --------------------------------------------------------------------------- #
# Churn
# --------------------------------------------------------------------------- #


def churn_by_month(conn: sqlite3.Connection, months: int = 12, as_of: str | None = None) -> list[Row]:
    """Cancelamentos por mês e taxa sobre a base ativa no início do mês."""
    ref = _as_of(as_of)
    out: list[Row] = []
    for back in range(months - 1, -1, -1):
        start = _month_start(ref, back)
        end = _month_start(ref, back - 1) if back > 0 else ref.isoformat()
        base = conn.execute(
            "SELECT COUNT(*) FROM customers WHERE signup_date < ? "
            "AND (cancel_date IS NULL OR cancel_date >= ?)",
            (start, start),
        ).fetchone()[0]
        cancelled = conn.execute(
            "SELECT COUNT(*) FROM customers WHERE cancel_date >= ? AND cancel_date < ?", (start, end)
        ).fetchone()[0]
        out.append(
            {
                "month": start[:7],
                "base_start": base,
                "cancelled": cancelled,
                "churn_rate_pct": round(100 * cancelled / base, 2) if base else 0.0,
            }
        )
    return out


def churn_reasons(conn: sqlite3.Connection, months: int = 6, as_of: str | None = None) -> list[Row]:
    """Motivos de cancelamento no período, com participação percentual."""
    ref = _as_of(as_of)
    since = _month_start(ref, months - 1)
    rows = _rows(
        conn.execute(
            "SELECT cancel_reason reason, COUNT(*) n FROM customers "
            "WHERE cancel_date >= ? AND cancel_date <= ? GROUP BY 1 ORDER BY 2 DESC",
            (since, ref.isoformat()),
        )
    )
    total = sum(r["n"] for r in rows) or 1
    for r in rows:
        r["share_pct"] = round(100 * r["n"] / total, 1)
    return rows


def churn_by_segment(conn: sqlite3.Connection, segment: str = "plan", as_of: str | None = None) -> list[Row]:
    """Churn acumulado por plano, cidade ou canal de venda."""
    column = {"plan": "p.name", "city": "c.city", "channel": "c.channel"}.get(segment)
    if column is None:
        raise ValueError("segment deve ser 'plan', 'city' ou 'channel'")
    return _rows(
        conn.execute(
            f"SELECT {column} segment, COUNT(*) customers, "
            f"SUM(c.status = 'cancelled') cancelled, "
            f"ROUND(100.0 * SUM(c.status = 'cancelled') / COUNT(*), 1) churn_pct "
            f"FROM customers c JOIN plans p ON p.id = c.plan_id "
            f"WHERE c.signup_date <= ? GROUP BY 1 ORDER BY churn_pct DESC",
            (_as_of(as_of).isoformat(),),
        )
    )


def churn_risk(conn: sqlite3.Connection, limit: int = 20, as_of: str | None = None) -> list[Row]:
    """Clientes ativos com maior risco de cancelamento, por pontuação de regras.

    Pontuação (0–100), transparente de propósito — não é um modelo de ML:
    - faturas em atraso nos últimos 90 dias: +15 cada (máx. 45)
    - chamados técnicos nos últimos 90 dias: +10 cada (máx. 30)
    - cliente com menos de 3 meses de casa: +15
    - plano de entrada (menor preço): +10
    """
    ref = _as_of(as_of)
    d90 = (ref - timedelta(days=90)).isoformat()
    d3m = (ref - timedelta(days=90)).isoformat()
    cheapest = conn.execute("SELECT id FROM plans ORDER BY price LIMIT 1").fetchone()[0]
    rows = _rows(
        conn.execute(
            """
            SELECT c.id, c.name, c.city, p.name plan, c.signup_date,
                   (SELECT COUNT(*) FROM invoices i WHERE i.customer_id = c.id
                      AND i.due_date >= ? AND i.due_date < ?
                      AND (i.paid_date IS NULL OR i.paid_date > i.due_date)) late_invoices,
                   (SELECT COUNT(*) FROM tickets t WHERE t.customer_id = c.id
                      AND t.opened_at >= ? AND t.category IN ('lentidao', 'sem_conexao')) tech_tickets
            FROM customers c JOIN plans p ON p.id = c.plan_id
            WHERE c.status = 'active'
            """,
            (d90, ref.isoformat(), d90),
        )
    )
    for r in rows:
        factors = []
        score = 0
        if r["late_invoices"]:
            pts = min(45, 15 * r["late_invoices"])
            score += pts
            factors.append(f"{r['late_invoices']} fatura(s) em atraso (+{pts})")
        if r["tech_tickets"]:
            pts = min(30, 10 * r["tech_tickets"])
            score += pts
            factors.append(f"{r['tech_tickets']} chamado(s) técnico(s) (+{pts})")
        if r["signup_date"] > d3m:
            score += 15
            factors.append("menos de 3 meses de casa (+15)")
        if r["plan"] == conn.execute("SELECT name FROM plans WHERE id = ?", (cheapest,)).fetchone()[0]:
            score += 10
            factors.append("plano de entrada (+10)")
        r["risk_score"] = score
        r["factors"] = factors
    rows.sort(key=lambda r: (-r["risk_score"], r["id"]))
    return rows[:limit]


# --------------------------------------------------------------------------- #
# Inadimplência
# --------------------------------------------------------------------------- #


def delinquency_aging(conn: sqlite3.Connection, as_of: str | None = None) -> list[Row]:
    """Faturas vencidas e não pagas por faixa de atraso (1–30, 31–60, 61–90, 90+ dias)."""
    ref = _as_of(as_of)
    rows = _rows(
        conn.execute(
            """
            SELECT CASE
                     WHEN julianday(?) - julianday(due_date) <= 30 THEN '01-30'
                     WHEN julianday(?) - julianday(due_date) <= 60 THEN '31-60'
                     WHEN julianday(?) - julianday(due_date) <= 90 THEN '61-90'
                     ELSE '90+' END bucket,
                   COUNT(*) invoices, COUNT(DISTINCT customer_id) customers, ROUND(SUM(amount), 2) amount
            FROM invoices WHERE paid_date IS NULL AND due_date < ?
            GROUP BY 1 ORDER BY 1
            """,
            (ref.isoformat(),) * 4,
        )
    )
    return rows


def delinquency_by_month(conn: sqlite3.Connection, months: int = 12, as_of: str | None = None) -> list[Row]:
    """Por mês de vencimento: % de faturas pagas com atraso ou não pagas."""
    ref = _as_of(as_of)
    since = _month_start(ref, months - 1)
    return _rows(
        conn.execute(
            """
            SELECT substr(due_date, 1, 7) month, COUNT(*) invoices,
                   SUM(paid_date IS NULL) unpaid,
                   SUM(paid_date IS NOT NULL AND paid_date > due_date) paid_late,
                   ROUND(100.0 * SUM(paid_date IS NULL OR paid_date > due_date) / COUNT(*), 1) late_pct
            FROM invoices WHERE due_date >= ? AND due_date <= ?
            GROUP BY 1 ORDER BY 1
            """,
            (since, ref.isoformat()),
        )
    )


def delinquent_customers(
    conn: sqlite3.Connection, min_days: int = 30, limit: int = 50, as_of: str | None = None
) -> list[Row]:
    """Clientes ativos com fatura vencida há pelo menos `min_days` dias, do maior valor para o menor."""
    ref = _as_of(as_of)
    cutoff = (ref - timedelta(days=min_days)).isoformat()
    return _rows(
        conn.execute(
            """
            SELECT c.id, c.name, c.city, COUNT(*) overdue_invoices, ROUND(SUM(i.amount), 2) amount,
                   MIN(i.due_date) oldest_due,
                   CAST(julianday(?) - julianday(MIN(i.due_date)) AS INTEGER) days_overdue
            FROM invoices i JOIN customers c ON c.id = i.customer_id
            WHERE i.paid_date IS NULL AND i.due_date <= ? AND c.status = 'active'
            GROUP BY c.id ORDER BY amount DESC, days_overdue DESC LIMIT ?
            """,
            (ref.isoformat(), cutoff, limit),
        )
    )


# --------------------------------------------------------------------------- #
# Retenção e vendas
# --------------------------------------------------------------------------- #


def retention_cohorts(conn: sqlite3.Connection, months: int = 12, as_of: str | None = None) -> list[Row]:
    """Coortes por mês de entrada: % ainda ativos após 1, 3, 6 e 12 meses."""
    ref = _as_of(as_of)
    since = _month_start(ref, months - 1)
    cohorts = _rows(
        conn.execute(
            "SELECT substr(signup_date, 1, 7) cohort, COUNT(*) size FROM customers "
            "WHERE signup_date >= ? AND signup_date <= ? GROUP BY 1 ORDER BY 1",
            (since, ref.isoformat()),
        )
    )
    for c in cohorts:
        for horizon in (1, 3, 6, 12):
            y, m = divmod(int(c["cohort"][5:]) - 1 + horizon, 12)
            checkpoint = date(int(c["cohort"][:4]) + y, m + 1, 1)
            if checkpoint > ref:
                c[f"m{horizon}_pct"] = None
                continue
            alive = conn.execute(
                "SELECT COUNT(*) FROM customers WHERE substr(signup_date, 1, 7) = ? "
                "AND (cancel_date IS NULL OR cancel_date >= ?)",
                (c["cohort"], checkpoint.isoformat()),
            ).fetchone()[0]
            c[f"m{horizon}_pct"] = round(100 * alive / c["size"], 1)
    return cohorts


def sales_by_month(
    conn: sqlite3.Connection, months: int = 6, group_by: str = "channel", as_of: str | None = None
) -> list[Row]:
    """Novos clientes por mês, agrupados por canal de venda, plano ou cidade."""
    column = {"channel": "c.channel", "plan": "p.name", "city": "c.city"}.get(group_by)
    if column is None:
        raise ValueError("group_by deve ser 'channel', 'plan' ou 'city'")
    ref = _as_of(as_of)
    since = _month_start(ref, months - 1)
    return _rows(
        conn.execute(
            f"SELECT substr(c.signup_date, 1, 7) month, {column} segment, COUNT(*) new_customers, "
            f"ROUND(SUM(p.price), 2) new_mrr FROM customers c JOIN plans p ON p.id = c.plan_id "
            f"WHERE c.signup_date >= ? AND c.signup_date <= ? GROUP BY 1, 2 ORDER BY 1, 3 DESC",
            (since, ref.isoformat()),
        )
    )


# --------------------------------------------------------------------------- #
# Suporte
# --------------------------------------------------------------------------- #


def ticket_stats(conn: sqlite3.Connection, months: int = 3, as_of: str | None = None) -> list[Row]:
    """Chamados por categoria e cidade: volume, % resolvidos e tempo médio de resolução (horas)."""
    ref = _as_of(as_of)
    since = _month_start(ref, months - 1)
    return _rows(
        conn.execute(
            """
            SELECT c.city, t.category, COUNT(*) tickets,
                   ROUND(100.0 * SUM(t.closed_at IS NOT NULL) / COUNT(*), 1) resolved_pct,
                   ROUND(AVG((julianday(t.closed_at) - julianday(t.opened_at)) * 24), 1) avg_hours
            FROM tickets t JOIN customers c ON c.id = t.customer_id
            WHERE t.opened_at >= ? AND t.opened_at <= ?
            GROUP BY 1, 2 ORDER BY tickets DESC
            """,
            (since, ref.isoformat() + "T23:59"),
        )
    )


def tickets_by_month(
    conn: sqlite3.Connection, months: int = 12, city: str | None = None, as_of: str | None = None
) -> list[Row]:
    """Volume mensal de chamados por categoria (opcionalmente filtrado por cidade) — bom para achar picos."""
    ref = _as_of(as_of)
    since = _month_start(ref, months - 1)
    sql = (
        "SELECT substr(t.opened_at, 1, 7) month, t.category, COUNT(*) tickets "
        "FROM tickets t JOIN customers c ON c.id = t.customer_id "
        "WHERE t.opened_at >= ? AND t.opened_at <= ?"
    )
    params: list[Any] = [since, ref.isoformat() + "T23:59"]
    if city:
        sql += " AND c.city = ?"
        params.append(city)
    sql += " GROUP BY 1, 2 ORDER BY 1, 3 DESC"
    return _rows(conn.execute(sql, params))


# --------------------------------------------------------------------------- #
# SQL livre (somente leitura, com guarda-corpo)
# --------------------------------------------------------------------------- #

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|replace|reindex)\b", re.I
)


def run_sql(conn: sqlite3.Connection, query: str, max_rows: int = 200) -> Row:
    """Executa um SELECT arbitrário com três camadas de proteção:

    1. a conexão é aberta em modo somente leitura (`mode=ro`);
    2. só uma instrução, começando por SELECT/WITH, sem palavras de escrita;
    3. limite de linhas devolvidas.
    """
    q = query.strip().rstrip(";").strip()
    if ";" in q:
        raise ValueError("apenas uma instrução por chamada")
    if not re.match(r"^(select|with)\b", q, re.I):
        raise ValueError("apenas consultas SELECT (ou WITH ... SELECT) são permitidas")
    if _FORBIDDEN.search(q):
        raise ValueError("a consulta contém palavras-chave de escrita, que não são permitidas")
    cur = conn.execute(q)
    rows = [dict(r) for r in cur.fetchmany(max_rows + 1)]
    truncated = len(rows) > max_rows
    return {
        "columns": [d[0] for d in cur.description] if cur.description else [],
        "rows": rows[:max_rows],
        "truncated": truncated,
    }
