"""DuckDB 连接管理与数据库结构定义。

路径解析优先级:--db 参数 > SCHWAB_DB 环境变量 > ~/.local/share/schwab/schwab.duckdb。
所有表使用 IF NOT EXISTS,connect() 时自动完成初始化,保证 CLI 任何子命令
首次运行即可用。
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

# 默认数据库文件位置(可用 --db 或 SCHWAB_DB 覆盖)
DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "schwab" / "schwab.duckdb"

# 建表 DDL:
# - import_files: 文件级导入登记(file_hash 为文件内容哈希,防止整文件重复导入)
# - transactions: 清洗后的统一交易表,txn_hash 为规范化行内容哈希,行级幂等去重;
#   seq 保留文件内原始行序(CSV 为最新在前),供 FIFO 重放确定同日先后
# - lots / realized: FIFO 引擎产物,由 rebuild 全量重建
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS import_files (
    file_hash   VARCHAR PRIMARY KEY,
    file_name   VARCHAR NOT NULL,
    account     VARCHAR NOT NULL,
    imported_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
    row_count   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    txn_hash    VARCHAR PRIMARY KEY,
    account     VARCHAR NOT NULL,
    record_date DATE NOT NULL,          -- CSV Date 列的前一个日期(无 as of 时与 txn_date 相同)
    txn_date    DATE NOT NULL,          -- 生效日期(as of 之后的日期)
    action      VARCHAR NOT NULL,
    raw_symbol  VARCHAR,                -- 原始 Symbol(期权为完整合约名)
    description VARCHAR,
    asset_type  VARCHAR NOT NULL,       -- stock | option | cash
    underlying  VARCHAR,                -- 股票代码 / 期权标的
    expiry      DATE,                   -- 期权到期日
    strike      DECIMAL(18,4),          -- 期权行权价
    option_type VARCHAR,                -- C | P
    quantity    DECIMAL(18,6),
    price       DECIMAL(18,6),
    fees        DECIMAL(18,6),
    amount      DECIMAL(18,4),
    source_hash VARCHAR NOT NULL,       -- 来源文件哈希
    seq         INTEGER NOT NULL        -- 文件内原始行号(1 起,含表头偏移)
);

CREATE SEQUENCE IF NOT EXISTS lots_seq START 1;
CREATE TABLE IF NOT EXISTS lots (
    lot_id        INTEGER PRIMARY KEY DEFAULT nextval('lots_seq'),
    account       VARCHAR NOT NULL,
    symbol_key    VARCHAR NOT NULL,     -- 股票为代码,期权为完整合约名
    asset_type    VARCHAR NOT NULL,     -- stock | option
    underlying    VARCHAR NOT NULL,
    expiry        DATE,
    strike        DECIMAL(18,4),
    option_type   VARCHAR,
    direction     VARCHAR NOT NULL,     -- long | short(期权 Sell to Open 为 short)
    open_date     DATE NOT NULL,
    open_price    DECIMAL(18,6) NOT NULL,
    open_qty      DECIMAL(18,6) NOT NULL,
    remaining_qty DECIMAL(18,6) NOT NULL,
    open_fees     DECIMAL(18,6) NOT NULL DEFAULT 0,
    open_txn      VARCHAR NOT NULL      -- 开仓交易 txn_hash
);

CREATE SEQUENCE IF NOT EXISTS realized_seq START 1;
CREATE TABLE IF NOT EXISTS realized (
    realized_id  INTEGER PRIMARY KEY DEFAULT nextval('realized_seq'),
    account      VARCHAR NOT NULL,
    symbol_key   VARCHAR NOT NULL,
    asset_type   VARCHAR NOT NULL,
    underlying   VARCHAR NOT NULL,
    direction    VARCHAR NOT NULL,      -- 被平掉的持仓方向
    open_date    DATE NOT NULL,
    close_date   DATE NOT NULL,
    qty          DECIMAL(18,6) NOT NULL,
    open_price   DECIMAL(18,6) NOT NULL,
    close_price  DECIMAL(18,6) NOT NULL,
    open_fees    DECIMAL(18,6) NOT NULL DEFAULT 0,  -- 按平仓数量比例分摊的开仓费
    close_fees   DECIMAL(18,6) NOT NULL DEFAULT 0,  -- 按平仓数量比例分摊的平仓费
    pnl          DECIMAL(18,4) NOT NULL,
    close_action VARCHAR NOT NULL,      -- 触发平仓的 Action(Sell / Expired / Assigned ...)
    open_txn     VARCHAR NOT NULL,
    close_txn    VARCHAR NOT NULL
);
"""

# 查询视图:当前持仓聚合 / 已实现损益月度汇总 / 现金流水
VIEWS_SQL = """
CREATE OR REPLACE VIEW v_positions AS
SELECT
    account,
    symbol_key,
    asset_type,
    underlying,
    expiry,
    strike,
    option_type,
    direction,
    sum(remaining_qty)                          AS qty,
    sum(remaining_qty * open_price)             AS cost,      -- 不含费用的持仓成本
    min(open_date)                              AS first_open_date
FROM lots
WHERE remaining_qty > 0
GROUP BY account, symbol_key, asset_type, underlying, expiry, strike, option_type, direction;

CREATE OR REPLACE VIEW v_realized_summary AS
SELECT
    account,
    underlying,
    asset_type,
    strftime(close_date, '%Y-%m') AS month,
    count(*)                      AS trades,
    sum(pnl)                      AS pnl,
    sum(open_fees + close_fees)   AS fees
FROM realized
GROUP BY account, underlying, asset_type, month;

CREATE OR REPLACE VIEW v_cashflows AS
SELECT account, txn_date, action, description, amount, txn_hash
FROM transactions
WHERE asset_type = 'cash';
"""


def resolve_db_path(db: str | None = None) -> Path:
    """按优先级解析数据库路径:显式参数 > 环境变量 > 默认路径。"""
    raw = db or os.environ.get("SCHWAB_DB")
    path = Path(raw).expanduser() if raw else DEFAULT_DB_PATH
    return path


def connect(db: str | None = None) -> duckdb.DuckDBPyConnection:
    """打开(必要时创建)数据库并初始化表结构,返回连接。"""
    path = resolve_db_path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(SCHEMA_SQL)
    con.execute(VIEWS_SQL)
    return con
