"""schwab CLI 入口(Typer)。

命令一览(全部支持 --json 输出,方便 AI 消费):
    schwab import <csv...>   清洗并幂等导入嘉信交易 CSV
    schwab rebuild           全量重放 FIFO,重建持仓与已实现损益
    schwab positions         当前持仓(股票股数 + 期权合约、成本、方向)
    schwab realized          已实现损益明细与合计
    schwab trades            交易流水查询
    schwab cashflow          出入金/股息/利息/税
    schwab summary           账户总览

每个命令都接受 --db(数据库路径,默认 SCHWAB_DB 环境变量,
再否则 ~/.local/share/schwab/schwab.duckdb)与 --json。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from . import db as dbmod
from .fifo import rebuild as fifo_rebuild
from .ingest import IngestError, import_csv

app = typer.Typer(help="嘉信(Schwab)个人交易记录:导入、FIFO 持仓与损益查询", no_args_is_help=True)
console = Console()
err_console = Console(stderr=True)

# 所有子命令共享的全局选项(放在子命令上,保证 `schwab <cmd> --json` 可用)
DbOption = Annotated[Optional[Path], typer.Option("--db", help="DuckDB 文件路径(默认 SCHWAB_DB 或 ~/.local/share/schwab/schwab.duckdb)")]
JsonOption = Annotated[bool, typer.Option("--json", help="以 JSON 输出(AI 友好)")]


def _emit(rows: list[dict], columns: list[str], title: str, as_json: bool,
          extra: dict | None = None) -> None:
    """统一出口:--json 时输出 JSON,否则渲染 rich 表格。

    参数:
        rows:     结果行(字典列表,键需覆盖 columns)
        columns:  展示列及顺序
        title:    表格标题
        as_json:  是否 JSON 输出
        extra:    JSON 模式下附带在 {"rows": ..., **extra} 的汇总信息
    """
    if as_json:
        payload: dict = {"rows": rows}
        if extra:
            payload.update(extra)
        typer.echo(json.dumps(payload, ensure_ascii=False, default=str))
        return
    table = Table(title=title)
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*[str(row.get(c, "")) if row.get(c) is not None else "" for c in columns])
    console.print(table)
    if extra:
        for key, value in extra.items():
            console.print(f"[bold]{key}[/]: {value}")


def _query(con, sql: str, params: list | None = None) -> list[dict]:
    """执行查询并返回字典行列表(键为列名)。"""
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


@app.command(name="import")
def import_cmd(
    files: Annotated[list[Path], typer.Argument(help="嘉信导出的 Transactions CSV(可多个)", exists=True)],
    db: DbOption = None,
    as_json: JsonOption = False,
) -> None:
    """清洗 CSV 并幂等导入 transactions 表(导入后需 rebuild 刷新持仓)。"""
    con = dbmod.connect(str(db) if db else None)
    results = []
    for path in files:
        try:
            results.append(import_csv(con, path))
        except IngestError as e:
            err_console.print(f"[red]导入失败[/] {path.name}: {e}")
            raise typer.Exit(code=1)
    if as_json:
        typer.echo(json.dumps({"files": results}, ensure_ascii=False, default=str))
    else:
        for r in results:
            console.print(
                f"{r['file']} (账户 {r['account']}): "
                f"共 {r['total_rows']} 行,新增 {r['inserted']},跳过重复 {r['skipped']}"
            )
            for action, count in sorted(r["by_action"].items()):
                console.print(f"  {action}: {count}")
        console.print("提示:持仓与损益需运行 `schwab rebuild` 刷新")


@app.command()
def rebuild(db: DbOption = None, as_json: JsonOption = False) -> None:
    """全量重放交易,重建 lots / realized(幂等)。"""
    con = dbmod.connect(str(db) if db else None)
    stats = fifo_rebuild(con)
    if as_json:
        typer.echo(json.dumps(stats, ensure_ascii=False, default=str))
    else:
        console.print(
            f"重放 {stats['transactions_replayed']} 条交易 -> "
            f"未平批次 {stats['open_lots']},已实现记录 {stats['realized_records']},"
            f"已实现损益合计 ${stats['total_realized_pnl']}"
        )
        for warning in stats["warnings"]:
            console.print(f"[yellow]警告[/] {warning}")


@app.command()
def positions(
    db: DbOption = None,
    as_json: JsonOption = False,
    underlying: Annotated[Optional[str], typer.Option("--underlying", "-u", help="按标的过滤(如 INTC)")] = None,
) -> None:
    """当前持仓:股票股数、期权合约、成本与方向。"""
    con = dbmod.connect(str(db) if db else None)
    sql = "SELECT * FROM v_positions"
    params: list = []
    if underlying:
        sql += " WHERE underlying = ?"
        params.append(underlying.upper())
    sql += " ORDER BY asset_type, underlying, expiry"
    rows = _query(con, sql, params)
    _emit(rows, ["underlying", "symbol_key", "asset_type", "direction", "qty", "cost",
                 "expiry", "strike", "option_type", "first_open_date"],
          "当前持仓", as_json, extra={"count": len(rows)})


@app.command()
def realized(
    db: DbOption = None,
    as_json: JsonOption = False,
    symbol: Annotated[Optional[str], typer.Option("--symbol", "-s", help="按标的/合约过滤")] = None,
    from_date: Annotated[Optional[str], typer.Option("--from", help="起始日期 YYYY-MM-DD")] = None,
    to_date: Annotated[Optional[str], typer.Option("--to", help="截止日期 YYYY-MM-DD")] = None,
) -> None:
    """已实现损益明细(含合计)。"""
    con = dbmod.connect(str(db) if db else None)
    sql = "SELECT * FROM realized WHERE 1=1"
    params: list = []
    if symbol:
        sql += " AND (underlying = ? OR symbol_key = ?)"
        params += [symbol.upper(), symbol.upper()]
    if from_date:
        sql += " AND close_date >= ?"
        params.append(from_date)
    if to_date:
        sql += " AND close_date <= ?"
        params.append(to_date)
    sql += " ORDER BY close_date DESC, realized_id DESC"
    rows = _query(con, sql, params)
    total = sum(r["pnl"] for r in rows)
    _emit(rows, ["close_date", "symbol_key", "direction", "qty", "open_price", "close_price",
                 "open_date", "pnl", "close_action"],
          "已实现损益", as_json, extra={"count": len(rows), "total_pnl": total})


@app.command()
def trades(
    db: DbOption = None,
    as_json: JsonOption = False,
    symbol: Annotated[Optional[str], typer.Option("--symbol", "-s", help="按标的/合约过滤")] = None,
    action: Annotated[Optional[str], typer.Option("--action", "-a", help="按 Action 过滤(如 Buy)")] = None,
    from_date: Annotated[Optional[str], typer.Option("--from", help="起始日期 YYYY-MM-DD")] = None,
    to_date: Annotated[Optional[str], typer.Option("--to", help="截止日期 YYYY-MM-DD")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="最多返回行数")] = 50,
) -> None:
    """交易流水查询(按生效日倒序)。"""
    con = dbmod.connect(str(db) if db else None)
    sql = "SELECT * FROM transactions WHERE 1=1"
    params: list = []
    if symbol:
        sql += " AND (underlying = ? OR raw_symbol = ?)"
        params += [symbol.upper(), symbol.upper()]
    if action:
        sql += " AND action = ?"
        params.append(action)
    if from_date:
        sql += " AND txn_date >= ?"
        params.append(from_date)
    if to_date:
        sql += " AND txn_date <= ?"
        params.append(to_date)
    sql += " ORDER BY txn_date DESC, seq ASC LIMIT ?"
    params.append(limit)
    rows = _query(con, sql, params)
    _emit(rows, ["txn_date", "action", "raw_symbol", "quantity", "price", "fees", "amount"],
          "交易流水", as_json, extra={"count": len(rows)})


@app.command()
def cashflow(
    db: DbOption = None,
    as_json: JsonOption = False,
    from_date: Annotated[Optional[str], typer.Option("--from", help="起始日期 YYYY-MM-DD")] = None,
    to_date: Annotated[Optional[str], typer.Option("--to", help="截止日期 YYYY-MM-DD")] = None,
) -> None:
    """现金流水:出入金、股息、利息、税(含分类合计)。"""
    con = dbmod.connect(str(db) if db else None)
    sql = "SELECT * FROM v_cashflows WHERE 1=1"
    params: list = []
    if from_date:
        sql += " AND txn_date >= ?"
        params.append(from_date)
    if to_date:
        sql += " AND txn_date <= ?"
        params.append(to_date)
    sql += " ORDER BY txn_date DESC"
    rows = _query(con, sql, params)
    by_action: dict[str, object] = {}
    for r in rows:
        by_action[r["action"]] = by_action.get(r["action"], 0) + r["amount"]
    total = sum(r["amount"] for r in rows)
    _emit(rows, ["txn_date", "action", "description", "amount"],
          "现金流水", as_json,
          extra={"count": len(rows), "total": total, "by_action": by_action})


@app.command()
def summary(db: DbOption = None, as_json: JsonOption = False) -> None:
    """账户总览:净入金、已实现损益、未平仓、费用与股息税项合计。"""
    con = dbmod.connect(str(db) if db else None)

    def one(sql: str):
        return con.execute(sql).fetchone()[0]

    data = {
        "net_deposits": one("SELECT coalesce(sum(amount),0) FROM transactions WHERE action = 'MoneyLink Transfer'"),
        "realized_pnl_stock": one("SELECT coalesce(sum(pnl),0) FROM realized WHERE asset_type = 'stock'"),
        "realized_pnl_option": one("SELECT coalesce(sum(pnl),0) FROM realized WHERE asset_type = 'option'"),
        "realized_pnl_total": one("SELECT coalesce(sum(pnl),0) FROM realized"),
        "open_positions": one("SELECT count(*) FROM v_positions"),
        "total_fees": one("SELECT coalesce(sum(fees),0) FROM transactions"),
        "dividends": one("SELECT coalesce(sum(amount),0) FROM transactions WHERE action IN ('Qualified Dividend','Non-Qualified Div','Pr Yr Non-Qual Div')"),
        "interest": one("SELECT coalesce(sum(amount),0) FROM transactions WHERE action = 'Credit Interest'"),
        "taxes": one("SELECT coalesce(sum(amount),0) FROM transactions WHERE action IN ('NRA Tax Adj','Pr Yr NRA Tax','Foreign Tax Paid')"),
        "cash_balance": one("SELECT coalesce(sum(amount),0) FROM transactions"),
        "txn_count": one("SELECT count(*) FROM transactions"),
        "date_range": one("SELECT min(txn_date) || ' ~ ' || max(txn_date) FROM transactions"),
    }
    if as_json:
        typer.echo(json.dumps(data, ensure_ascii=False, default=str))
    else:
        table = Table(title="账户总览")
        table.add_column("指标")
        table.add_column("值")
        labels = {
            "net_deposits": "净入金(MoneyLink)",
            "realized_pnl_stock": "已实现损益-股票",
            "realized_pnl_option": "已实现损益-期权",
            "realized_pnl_total": "已实现损益-合计",
            "open_positions": "未平仓数量",
            "total_fees": "费用合计",
            "dividends": "股息合计",
            "interest": "利息合计",
            "taxes": "税项合计",
            "cash_balance": "推算现金余额(Σamount)",
            "txn_count": "交易记录数",
            "date_range": "数据区间",
        }
        for key, label in labels.items():
            table.add_row(label, str(data[key]))
        console.print(table)


if __name__ == "__main__":
    app()
