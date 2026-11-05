"""Conexão e esquema do banco SQLite.

O caminho do banco vem de `ISP_DB_PATH` (padrão: `data/aurora.db` na raiz do
projeto). O esquema é propositalmente simples — é o recorte mínimo de um ERP de
provedor que permite responder perguntas de churn, inadimplência, retenção e
vendas.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB = ROOT / "data" / "aurora.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    speed_mbps  INTEGER NOT NULL,
    price       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    city           TEXT NOT NULL,
    plan_id        INTEGER NOT NULL REFERENCES plans(id),
    channel        TEXT NOT NULL,            -- site | whatsapp | indicacao | loja
    signup_date    TEXT NOT NULL,            -- ISO (YYYY-MM-DD)
    status         TEXT NOT NULL,            -- active | cancelled
    cancel_date    TEXT,
    cancel_reason  TEXT                      -- preco | mudanca | concorrente | qualidade | inadimplencia
);

CREATE TABLE IF NOT EXISTS invoices (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    due_date     TEXT NOT NULL,
    paid_date    TEXT,
    amount       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    opened_at    TEXT NOT NULL,              -- ISO datetime
    closed_at    TEXT,
    category     TEXT NOT NULL               -- lentidao | sem_conexao | financeiro | instalacao | outro
);

CREATE INDEX IF NOT EXISTS idx_customers_status ON customers(status);
CREATE INDEX IF NOT EXISTS idx_invoices_customer ON invoices(customer_id);
CREATE INDEX IF NOT EXISTS idx_invoices_due ON invoices(due_date);
CREATE INDEX IF NOT EXISTS idx_tickets_customer ON tickets(customer_id);
"""


def db_path() -> Path:
    return Path(os.getenv("ISP_DB_PATH", str(DEFAULT_DB)))


def connect(path: Path | None = None, *, readonly: bool = True) -> sqlite3.Connection:
    """Abre o banco. Por padrão em modo somente leitura — o servidor nunca escreve."""
    path = path or db_path()
    if readonly:
        if not path.exists():
            raise FileNotFoundError(
                f"banco não encontrado em {path}. Rode `isp-analytics-seed` para gerar os dados."
            )
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
