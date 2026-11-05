import pytest

from isp_analytics_mcp import queries as q

AS_OF = "2026-11-01"


def test_overview_is_consistent(conn):
    o = q.overview(conn, AS_OF)
    assert o["active_customers"] > 0
    assert o["mrr"] == pytest.approx(o["arpu"] * o["active_customers"], rel=1e-3)
    assert 0 <= o["churn_rate_30d_pct"] <= 100
    assert o["overdue_customers"] <= o["active_customers"] + o["cancelled_last_30d"] + 10_000


def test_churn_by_month_shape(conn):
    rows = q.churn_by_month(conn, months=6, as_of=AS_OF)
    assert [r["month"] for r in rows] == ["2026-06", "2026-07", "2026-08", "2026-09", "2026-10", "2026-11"]
    assert all(r["cancelled"] <= r["base_start"] for r in rows)


def test_churn_reasons_shares_sum_to_100(conn):
    rows = q.churn_reasons(conn, months=12, as_of=AS_OF)
    assert rows and abs(sum(r["share_pct"] for r in rows) - 100) < 1


def test_churn_by_segment_rejects_unknown(conn):
    with pytest.raises(ValueError):
        q.churn_by_segment(conn, "cpf")


def test_churn_risk_scores_are_explained(conn):
    rows = q.churn_risk(conn, limit=10, as_of=AS_OF)
    assert len(rows) == 10
    assert rows == sorted(rows, key=lambda r: (-r["risk_score"], r["id"]))
    for r in rows:
        assert 0 <= r["risk_score"] <= 100
        assert r["factors"]  # todo score vem com justificativa


def test_delinquency_aging_buckets(conn):
    rows = q.delinquency_aging(conn, AS_OF)
    assert {r["bucket"] for r in rows} <= {"01-30", "31-60", "61-90", "90+"}
    assert all(r["invoices"] >= r["customers"] for r in rows)


def test_delinquency_seasonality(conn):
    rows = {r["month"]: r["late_pct"] for r in q.delinquency_by_month(conn, months=12, as_of=AS_OF)}
    assert rows["2026-02"] > rows["2026-06"]  # início de ano é pior (plantado no seed)


def test_delinquent_customers_only_active_and_ordered(conn):
    rows = q.delinquent_customers(conn, min_days=30, limit=20, as_of=AS_OF)
    amounts = [r["amount"] for r in rows]
    assert amounts == sorted(amounts, reverse=True)
    assert all(r["days_overdue"] >= 30 for r in rows)


def test_retention_cohorts_future_horizons_are_none(conn):
    rows = q.retention_cohorts(conn, months=6, as_of=AS_OF)
    last = rows[-1]
    assert last["m12_pct"] is None
    for r in rows:
        if r["m1_pct"] is not None and r["m3_pct"] is not None:
            assert r["m3_pct"] <= r["m1_pct"]


def test_sales_by_month_groupings(conn):
    for g in ("channel", "plan", "city"):
        rows = q.sales_by_month(conn, months=3, group_by=g, as_of=AS_OF)
        assert all(r["new_customers"] > 0 for r in rows)
    with pytest.raises(ValueError):
        q.sales_by_month(conn, group_by="cpf")


def test_tickets_spike_is_visible(conn):
    rows = q.tickets_by_month(conn, months=12, city="Monte Claro", as_of=AS_OF)
    lentidao = {r["month"]: r["tickets"] for r in rows if r["category"] == "lentidao"}
    assert lentidao["2026-07"] > 2 * max(v for m, v in lentidao.items() if m != "2026-07")


def test_ticket_stats_columns(conn):
    rows = q.ticket_stats(conn, months=3, as_of=AS_OF)
    assert rows and {"city", "category", "tickets", "resolved_pct", "avg_hours"} <= set(rows[0])


# ---------------------------------------------------------------- run_sql


def test_run_sql_select_works_and_truncates(conn):
    out = q.run_sql(conn, "SELECT id, name FROM customers ORDER BY id", max_rows=5)
    assert out["columns"] == ["id", "name"]
    assert len(out["rows"]) == 5 and out["truncated"] is True


@pytest.mark.parametrize(
    "bad",
    [
        "DELETE FROM customers",
        "SELECT 1; DROP TABLE customers",
        "PRAGMA writable_schema = 1",
        "WITH x AS (SELECT 1) INSERT INTO plans SELECT * FROM x",
        "ATTACH DATABASE '/tmp/x.db' AS x",
    ],
)
def test_run_sql_blocks_writes(conn, bad):
    with pytest.raises(ValueError):
        q.run_sql(conn, bad)
