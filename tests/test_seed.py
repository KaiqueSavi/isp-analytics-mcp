import sqlite3

from isp_analytics_mcp.db import connect
from isp_analytics_mcp.seed import generate


def test_seed_is_deterministic(tmp_path):
    def build(name: str) -> list[tuple]:
        conn = connect(tmp_path / name, readonly=False)
        generate(conn, customers=50, months=6, seed=1)
        rows = conn.execute("SELECT * FROM customers ORDER BY id").fetchall()
        conn.close()
        return [tuple(r) for r in rows]

    assert build("a.db") == build("b.db")


def test_seed_has_planted_patterns(conn: sqlite3.Connection):
    # plano mais barato cancela mais que o mais caro
    churn = {
        r["name"]: r["pct"]
        for r in conn.execute(
            "SELECT p.name, 100.0 * SUM(c.status='cancelled') / COUNT(*) pct "
            "FROM customers c JOIN plans p ON p.id = c.plan_id GROUP BY 1"
        )
    }
    assert churn["Essencial 200"] > churn["Ultra 1G"]

    # cancelados sempre têm data e motivo; ativos nunca
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM customers WHERE status='cancelled' "
            "AND (cancel_date IS NULL OR cancel_reason IS NULL)"
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM customers WHERE status='active' AND cancel_date IS NOT NULL"
        ).fetchone()[0]
        == 0
    )

    # nenhuma fatura ou chamado antes da entrada do cliente
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM invoices i JOIN customers c ON c.id = i.customer_id "
            "WHERE i.due_date < c.signup_date"
        ).fetchone()[0]
        == 0
    )


def test_readonly_connection_rejects_writes(db_file):
    conn = connect(__import__("pathlib").Path(db_file))
    try:
        with __import__("pytest").raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM customers")
    finally:
        conn.close()
