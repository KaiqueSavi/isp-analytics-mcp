# isp-analytics-mcp

Servidor **MCP (Model Context Protocol)** que dá a um agente de IA acesso analítico
aos dados de um provedor de internet: **churn, inadimplência, retenção por coorte,
vendas por canal e chamados de suporte** — sobre um banco **100% sintético** de
uma operadora fictícia, a *Aurora Fibra*.

![CI](https://github.com/KaiqueSavi/isp-analytics-mcp/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![MCP SDK](https://img.shields.io/badge/mcp--sdk-2.x-purple)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

> Versão pública de um padrão que uso em produção: em vez de dar ao modelo um
> dump do banco, dar a ele **ferramentas com semântica de negócio** (`churn_by_month`,
> `delinquency_aging`…) e um `run_sql` com guarda-corpo para o que as ferramentas
> não cobrem. Nenhum dado, nome ou esquema aqui vem de um sistema real.

---

## O que o agente consegue fazer com isso

```
Você:    Por que o churn subiu nos últimos meses?
Agente:  [overview] → [churn_by_month 12] → [churn_by_segment plan] → [churn_by_segment city]
         → [tickets_by_month city="Monte Claro"]
         "O churn de 30 dias está em 2,4%. O plano Essencial 200 tem 30% de churn
          acumulado, quase 3× o Ultra 1G (11%). Monte Claro teve 219 chamados de
          lentidão em julho/2026 contra ~15 por mês no resto do ano — vale cruzar
          com os cancelamentos por 'qualidade' nos meses seguintes…"
```

Esses padrões existem de propósito no gerador de dados (ver [Dados sintéticos](#dados-sintéticos)),
para que haja o que descobrir.

---

## Tools

| Tool | O que responde |
|---|---|
| `overview(as_of)` | Base ativa, MRR, ARPU, churn 30d, inadimplência e chamados abertos |
| `churn_by_month(months)` | Cancelamentos e taxa mês a mês (sobre a base do início do mês) |
| `churn_reasons(months)` | Motivos de cancelamento com participação % |
| `churn_by_segment(segment)` | Churn acumulado por `plan`, `city` ou `channel` |
| `churn_risk(limit)` | Clientes ativos com maior risco, com pontuação 0–100 **e os fatores** |
| `delinquency_aging()` | Faturas vencidas por faixa: 01-30, 31-60, 61-90, 90+ dias |
| `delinquency_by_month(months)` | % de faturas pagas com atraso ou em aberto, por mês de vencimento |
| `delinquent_customers(min_days, limit)` | Clientes ativos em atraso, do maior valor para o menor |
| `retention_cohorts(months)` | Coortes por mês de entrada: % ativos após 1, 3, 6 e 12 meses |
| `sales_by_month(months, group_by)` | Novos clientes e MRR novo por `channel`, `plan` ou `city` |
| `ticket_stats(months)` | Chamados por cidade/categoria, % resolvidos e tempo médio |
| `tickets_by_month(months, city)` | Volume mensal por categoria — para achar picos |
| `run_sql(query, max_rows)` | `SELECT` livre, somente leitura, com guarda-corpo |

Todas aceitam `as_of` (data de referência) para respostas reprodutíveis.

**Resources:** `isp://schema` (DDL das tabelas) e `isp://glossary` (definição de cada métrica).
**Prompt:** `monthly_review` — roteiro de revisão mensal que encadeia as tools na ordem certa.

---

## Como rodar

```bash
git clone https://github.com/KaiqueSavi/isp-analytics-mcp
cd isp-analytics-mcp
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

isp-analytics-seed            # gera data/aurora.db (1.500 clientes, 24 meses, seed fixa)
pytest -q                     # 28 testes, incluindo o protocolo MCP em processo
isp-analytics-mcp             # sobe o servidor via stdio
```

### Conectar ao Claude Desktop

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "isp-analytics": {
      "command": "/caminho/para/isp-analytics-mcp/.venv/bin/isp-analytics-mcp",
      "env": { "ISP_DB_PATH": "/caminho/para/isp-analytics-mcp/data/aurora.db" }
    }
  }
}
```

### Conectar ao Claude Code

```bash
claude mcp add isp-analytics -e ISP_DB_PATH=$PWD/data/aurora.db -- $PWD/.venv/bin/isp-analytics-mcp
```

### Testar com o MCP Inspector

```bash
npx @modelcontextprotocol/inspector .venv/bin/isp-analytics-mcp
```

### Via HTTP

```bash
isp-analytics-mcp --http      # streamable-http em http://localhost:8000/mcp
```

---

## Decisões de projeto

**Tools com semântica de negócio, não só SQL.** Um agente que só tem `run_sql`
precisa adivinhar o esquema, a definição de churn e a data de referência a cada
pergunta — e erra. Cada tool aqui encapsula uma definição (documentada em
`isp://glossary`) e devolve números prontos para comparar.

**`run_sql` existe, mas com três camadas de proteção:** a conexão é aberta em modo
somente leitura (`mode=ro`), a consulta precisa começar por `SELECT`/`WITH` sem
palavras-chave de escrita e sem `;`, e o número de linhas é limitado. Os testes
cobrem `DELETE`, `DROP` encadeado, `PRAGMA`, `ATTACH` e `WITH … INSERT`.

**Risco de churn por regras, não por modelo.** A pontuação (`churn_risk`) soma
fatores explícitos — faturas em atraso, chamados técnicos, tempo de casa, plano de
entrada — e devolve a lista de fatores junto com a nota. Em um cenário real o passo
seguinte é trocar por um modelo treinado (XGBoost + SHAP, por exemplo) mantendo a
mesma interface: a tool devolve `risk_score` e `factors` de qualquer jeito.

**Consultas separadas do servidor.** `queries.py` são funções puras sobre uma
conexão SQLite; `server.py` só registra cada uma como tool. Isso permite testar a
lógica sem protocolo e testar o protocolo sem lógica (`tests/test_server.py` sobe o
servidor em processo com `mcp.Client`).

**Erros chegam ao modelo com mensagem.** Validações (`ValueError`) e erros de SQL
viram `ToolError`, então o agente lê "apenas consultas SELECT são permitidas" e se
corrige, em vez de receber um erro genérico.

**Data de referência explícita.** Toda tool aceita `as_of`; o padrão é a data
"hoje" do dataset (2026-11-01). Nada depende do relógio da máquina.

---

## Dados sintéticos

`isp-analytics-seed` gera, de forma determinística (seed 42), uma operadora com
4 planos, 4 cidades, 4 canais de venda e 24 meses de histórico. O gerador planta
correlações para que as análises tenham o que encontrar:

- clientes com mais chamados técnicos e faturas atrasadas cancelam mais;
- o plano de entrada tem churn ~3× maior que o plano topo; quem veio por **indicação** fica mais;
- a inadimplência sobe em **janeiro–março** (~38% vs. ~25% no resto do ano);
- **Monte Claro** tem um pico de chamados de *lentidão* em **julho/2026**;
- os motivos de cancelamento são coerentes com o histórico (quem tinha problema técnico cancela por "qualidade").

Esquema (SQLite):

```
plans      (id, name, speed_mbps, price)
customers  (id, name, city, plan_id, channel, signup_date, status, cancel_date, cancel_reason)
invoices   (id, customer_id, due_date, paid_date, amount)
tickets    (id, customer_id, opened_at, closed_at, category)
```

Nomes de pessoas são combinações aleatórias de listas de nomes comuns; cidades,
planos e a operadora são inventados.

---

## Estrutura

```
src/isp_analytics_mcp/
  db.py        conexão (somente leitura por padrão) e DDL
  seed.py      gerador de dados sintéticos
  queries.py   consultas analíticas (funções puras, testáveis)
  server.py    MCPServer: tools, resources, prompt
tests/
  test_seed.py      determinismo, padrões plantados, bloqueio de escrita
  test_queries.py   cada consulta + guarda-corpo do run_sql
  test_server.py    protocolo MCP em processo (list_tools, call_tool, resources, prompt)
```

---

## Limitações e próximos passos

- SQLite e consultas síncronas: suficiente para o dataset, não para um ERP real
  com milhões de faturas — ali entra um banco analítico e cache de agregações.
- A pontuação de risco é heurística; o próximo passo natural é um modelo treinado
  nas mesmas features, com explicabilidade por cliente.
- Sem autenticação no modo HTTP; para expor fora do localhost, colocar atrás de um
  proxy com OAuth (o SDK suporta `token_verifier`).

---

## Licença

MIT — veja [LICENSE](LICENSE).
