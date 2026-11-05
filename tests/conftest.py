import sqlite3

import pytest

from isp_analytics_mcp.db import connect
from isp_analytics_mcp.seed import generate


@pytest.fixture(scope="session")
def db_file(tmp_path_factory) -> str:
    """Banco sintético pequeno, gerado uma vez por sessão de testes."""
    path = tmp_path_factory.mktemp("db") / "aurora-test.db"
    conn = connect(path, readonly=False)
    generate(conn, customers=400, months=18, seed=7)
    conn.close()
    return str(path)


@pytest.fixture
def conn(db_file) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


@pytest.fixture
def server_env(db_file, monkeypatch):
    monkeypatch.setenv("ISP_DB_PATH", db_file)
