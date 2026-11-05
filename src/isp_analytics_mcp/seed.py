"""Gera um banco sintético de um provedor de internet fictício ("Aurora Fibra").

Determinístico (seed fixa) e com correlações de propósito, para que as
análises tenham o que encontrar:

- clientes com mais chamados técnicos e faturas atrasadas cancelam mais;
- planos mais baratos têm churn maior; quem veio por indicação fica mais;
- inadimplência sobe no início do ano (IPVA, IPTU, material escolar);
- um pico de chamados de "lentidão" em uma cidade em um mês específico.

Uso: `isp-analytics-seed [--customers 1500] [--months 24] [--out data/aurora.db]`
"""

from __future__ import annotations

import argparse
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from .db import connect, create_schema, db_path

TODAY = date(2026, 11, 1)  # "hoje" fixo, para o dataset ser reprodutível

FIRST_NAMES = [
    "Ana",
    "Bruno",
    "Carla",
    "Daniel",
    "Eduarda",
    "Felipe",
    "Gabriela",
    "Henrique",
    "Isabela",
    "João",
    "Karina",
    "Lucas",
    "Mariana",
    "Nicolas",
    "Olívia",
    "Pedro",
    "Rafaela",
    "Samuel",
    "Tatiane",
    "Vinícius",
    "Yasmin",
    "Thiago",
    "Larissa",
    "Marcos",
    "Beatriz",
    "Rodrigo",
]
LAST_NAMES = [
    "Silva",
    "Santos",
    "Oliveira",
    "Souza",
    "Lima",
    "Pereira",
    "Ferreira",
    "Costa",
    "Rodrigues",
    "Almeida",
    "Nascimento",
    "Araújo",
    "Carvalho",
    "Gomes",
    "Martins",
    "Ribeiro",
    "Barbosa",
]
CITIES = {"Serra Azul": 0.40, "Vale Verde": 0.30, "Monte Claro": 0.20, "Rio Manso": 0.10}
CHANNELS = {"whatsapp": 0.40, "site": 0.30, "indicacao": 0.20, "loja": 0.10}
PLANS = [  # (nome, mbps, preço, churn base mensal)
    ("Essencial 200", 200, 79.90, 0.020),
    ("Família 400", 400, 99.90, 0.014),
    ("Turbo 600", 600, 129.90, 0.010),
    ("Ultra 1G", 1000, 169.90, 0.007),
]
TICKET_CATEGORIES = {
    "lentidao": 0.35,
    "sem_conexao": 0.30,
    "financeiro": 0.15,
    "instalacao": 0.10,
    "outro": 0.10,
}


def _weighted(rng: random.Random, table: dict[str, float]) -> str:
    return rng.choices(list(table), weights=list(table.values()), k=1)[0]


def _poisson(rng: random.Random, lam: float) -> int:
    """Amostra Poisson (algoritmo de Knuth); suficiente para médias pequenas."""
    if lam <= 0:
        return 0
    limit, k, p = pow(2.718281828, -lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _month_add(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, min(d.day, 28))


def generate(conn: sqlite3.Connection, *, customers: int = 1500, months: int = 24, seed: int = 42) -> dict:
    rng = random.Random(seed)
    create_schema(conn)
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO plans(id, name, speed_mbps, price) VALUES (?,?,?,?)",
        [(i + 1, n, s, p) for i, (n, s, p, _) in enumerate(PLANS)],
    )

    start = _month_add(TODAY, -months)
    customer_rows, invoice_rows, ticket_rows = [], [], []
    invoice_id = ticket_id = 0

    for cid in range(1, customers + 1):
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        city = _weighted(rng, CITIES)
        channel = _weighted(rng, CHANNELS)
        plan_idx = rng.choices(range(len(PLANS)), weights=[0.35, 0.35, 0.20, 0.10], k=1)[0]
        plan_name, _, price, base_churn = PLANS[plan_idx]
        signup = start + timedelta(days=rng.randrange(0, (TODAY - start).days - 30))

        # Propensões individuais (latentes) que alimentam chamados, atraso e churn.
        trouble = rng.betavariate(1.2, 6)  # maioria com poucos problemas, cauda longa
        if city == "Monte Claro":
            trouble = min(1.0, trouble + 0.08)
        pays_late = rng.betavariate(1.5, 5)
        loyalty = 1.3 if channel == "indicacao" else 1.0

        status, cancel_date, cancel_reason = "active", None, None
        # Primeira fatura vence no dia 10 seguinte à entrada (nunca antes dela).
        first_due = signup.replace(day=10) if signup.day <= 10 else _month_add(signup, 1).replace(day=10)
        month_cursor = signup
        month_index = 0
        while month_cursor < TODAY:
            # Fatura do mês
            invoice_id += 1
            due = _month_add(first_due, month_index)
            if due > TODAY:
                break
            seasonal = 1.6 if due.month in (1, 2, 3) else 1.0
            late_prob = min(0.9, pays_late * 1.1 * seasonal)
            if rng.random() < late_prob:
                delay = int(rng.expovariate(1 / 18)) + 1
                paid = due + timedelta(days=delay)
                paid_date = paid.isoformat() if paid <= TODAY and rng.random() > 0.05 else None
            else:
                paid_date = (due - timedelta(days=rng.randrange(0, 6))).isoformat()
            invoice_rows.append((invoice_id, cid, due.isoformat(), paid_date, price))

            # Chamados do mês
            lam = trouble * 0.9
            if city == "Monte Claro" and due.year == 2026 and due.month == 7:
                lam += 1.5  # incidente de rede em julho/2026
            n_tickets = min(4, _poisson(rng, lam))
            for _ in range(n_tickets):
                ticket_id += 1
                opened = datetime.combine(due, datetime.min.time()) + timedelta(
                    days=rng.randrange(0, 28), hours=rng.randrange(7, 22)
                )
                if opened.date() > TODAY:
                    continue
                cat = _weighted(rng, TICKET_CATEGORIES)
                if city == "Monte Claro" and due.year == 2026 and due.month == 7:
                    cat = "lentidao" if rng.random() < 0.7 else cat
                hours = rng.lognormvariate(2.6, 0.7)  # mediana ~13h
                closed = opened + timedelta(hours=hours)
                closed_at = closed.isoformat(timespec="minutes") if closed.date() <= TODAY else None
                ticket_rows.append((ticket_id, cid, opened.isoformat(timespec="minutes"), closed_at, cat))

            # Churn do mês
            tenure_factor = 1.5 if month_index < 3 else (1.0 if month_index < 12 else 0.7)
            churn_prob = base_churn * tenure_factor / loyalty
            churn_prob *= 1 + 3 * trouble + 1.5 * (1 if paid_date is None else 0)
            if rng.random() < min(0.35, churn_prob):
                status = "cancelled"
                cancel_date = (due + timedelta(days=rng.randrange(5, 25))).isoformat()
                reasons = {
                    "preco": 0.30,
                    "mudanca": 0.20,
                    "concorrente": 0.20,
                    "qualidade": 0.15,
                    "inadimplencia": 0.15,
                }
                if trouble > 0.3:
                    reasons["qualidade"] += 0.4
                if paid_date is None:
                    reasons["inadimplencia"] += 0.5
                cancel_reason = _weighted(rng, reasons)
                break

            month_index += 1
            month_cursor = _month_add(signup, month_index)

        customer_rows.append(
            (cid, name, city, plan_idx + 1, channel, signup.isoformat(), status, cancel_date, cancel_reason)
        )

    cur.executemany(
        "INSERT INTO customers(id, name, city, plan_id, channel, signup_date, status, "
        "cancel_date, cancel_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        customer_rows,
    )
    cur.executemany(
        "INSERT INTO invoices(id, customer_id, due_date, paid_date, amount) VALUES (?,?,?,?,?)", invoice_rows
    )
    cur.executemany(
        "INSERT INTO tickets(id, customer_id, opened_at, closed_at, category) VALUES (?,?,?,?,?)", ticket_rows
    )
    conn.commit()
    return {"customers": len(customer_rows), "invoices": len(invoice_rows), "tickets": len(ticket_rows)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gera o banco sintético da Aurora Fibra.")
    parser.add_argument("--customers", type=int, default=1500)
    parser.add_argument("--months", type=int, default=24)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=None, help="caminho do .db (padrão: ISP_DB_PATH)")
    args = parser.parse_args(argv)

    out = args.out or db_path()
    if out.exists():
        out.unlink()
    conn = connect(out, readonly=False)
    stats = generate(conn, customers=args.customers, months=args.months, seed=args.seed)
    conn.close()
    print(f"banco gerado em {out}: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
