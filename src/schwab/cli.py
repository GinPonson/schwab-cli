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

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from . import db as dbmod
from .email_ingest import (
    account_suffix,
    load_gmail_content,
    load_gmail_messages,
    parse_econfirm,
    write_transactions_csv,
)
from .fifo import FifoError, rebuild as fifo_rebuild
from .ingest import IngestError, import_csv, parse_csv

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


def _exit_error(*, code: str, message: str, as_json: bool) -> None:
    """以稳定协议输出已知业务错误并使用非零状态码退出。

    参数:
        code:     供 AI 或脚本判断错误类型的稳定代码。
        message:  面向用户的完整错误信息。
        as_json:  为 True 时把结构化错误写到 stdout，否则写到 stderr。
    """
    if as_json:
        typer.echo(json.dumps({"ok": False, "error": {"code": code, "message": message}},
                              ensure_ascii=False))
    else:
        err_console.print(f"[red]{code}[/] {message}")
    raise typer.Exit(code=1)


@app.command(name="import")
def import_cmd(
    files: Annotated[list[Path], typer.Argument(help="嘉信导出的 Transactions CSV(可多个)", exists=True)],
    db: DbOption = None,
    as_json: JsonOption = False,
) -> None:
    """清洗 CSV 并幂等导入 transactions 表(导入后需 rebuild 刷新持仓)。"""
    con = dbmod.connect(str(db) if db else None)
    results = []
    # 先验证全部文件，避免第二个文件格式错误时第一个文件已经写入。
    try:
        for path in files:
            parse_csv(path)
    except IngestError as exc:
        _exit_error(code="INGEST_VALIDATION_ERROR", message=str(exc), as_json=as_json)

    for path in files:
        try:
            results.append(import_csv(con, path))
        except IngestError as exc:
            _exit_error(code="INGEST_VALIDATION_ERROR", message=str(exc), as_json=as_json)
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


def _resolve_email_account(con, suffix: str, explicit_account: str | None) -> str:
    """把邮件账户尾号解析为 CLI 使用的完整匿名账户标识。

    参数:
        con:              已打开的 DuckDB 连接。
        suffix:           eConfirm 正文中的账户尾号。
        explicit_account: 用户通过 ``--account`` 指定的账户；为空时从现有交易解析。

    返回:
        与现有数据库一致的完整账户标识。
    """
    if explicit_account:
        if not explicit_account.endswith(suffix):
            raise IngestError(
                f"--account {explicit_account!r} 与邮件账户尾号 {suffix!r} 不匹配"
            )
        return explicit_account
    rows = con.execute(
        "SELECT DISTINCT account FROM transactions WHERE right(account, ?) = ? ORDER BY account",
        [len(suffix), suffix],
    ).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    if not rows:
        raise IngestError(
            f"数据库中没有尾号为 {suffix!r} 的账户；首次邮件导入必须提供 --account"
        )
    raise IngestError(
        f"数据库中有多个尾号为 {suffix!r} 的账户；请使用 --account 明确指定"
    )


@app.command(name="import-email")
def import_email_cmd(
    files: Annotated[list[str], typer.Argument(
        help="Gmail API JSON、原始 .eml，或用 - 从 stdin 读取"
    )],
    db: DbOption = None,
    as_json: JsonOption = False,
    account: Annotated[Optional[str], typer.Option(
        "--account", help="完整账户标识；首次导入或尾号不唯一时必填"
    )] = None,
    rebuild_after: Annotated[bool, typer.Option(
        "--rebuild/--no-rebuild", help="导入成功后立即重建 FIFO 持仓与损益"
    )] = True,
) -> None:
    """直接解析 Gmail API JSON 或原始 MIME 中的 eConfirm 并幂等导入。"""
    con = dbmod.connect(str(db) if db else None)
    prepared: list[tuple[str, str, list, list[str]]] = []
    try:
        # 先解析并校验全部输入，任一邮件异常时不导入任何文件。
        if files.count("-") > 1:
            raise IngestError("标准输入 - 只能出现一次")
        for source in files:
            if source == "-":
                stdin_stream = getattr(sys.stdin, "buffer", sys.stdin)
                stdin_data = stdin_stream.read()
                if isinstance(stdin_data, str):
                    stdin_data = stdin_data.encode()
                messages = load_gmail_content(stdin_data, source_name="stdin")
                source_name = "stdin"
            else:
                path = Path(source)
                if not path.is_file():
                    raise IngestError(f"Gmail 输入文件不存在: {source}")
                messages = load_gmail_messages(path)
                source_name = str(path)
            suffixes = {account_suffix(message) for message in messages}
            if len(suffixes) != 1:
                raise IngestError(
                    f"{source_name}: 单个输入包含多个 Schwab 账户: {sorted(suffixes)}"
                )
            resolved_account = _resolve_email_account(con, suffixes.pop(), account)
            trades = []
            for message in messages:
                trades.extend(parse_econfirm(message))
            # 官方 CSV 是日期倒序；Python 稳定排序会保留邮件内成交展示顺序。
            trades.sort(key=lambda trade: trade.trade_date, reverse=True)
            prepared.append((
                source_name, resolved_account, trades,
                [message.message_id for message in messages],
            ))
    except IngestError as exc:
        _exit_error(code="EMAIL_INGEST_VALIDATION_ERROR", message=str(exc), as_json=as_json)

    results = []
    with tempfile.TemporaryDirectory(prefix="schwab-email-") as temp_dir:
        for source_name, resolved_account, trades, message_ids in prepared:
            id_hash = hashlib.sha256("|".join(message_ids).encode()).hexdigest()[:16]
            csv_path = Path(temp_dir) / (
                f"Individual_{resolved_account}_Transactions_GMAIL_{id_hash}.csv"
            )
            write_transactions_csv(csv_path, trades)
            try:
                result = import_csv(con, csv_path)
            except IngestError as exc:
                _exit_error(code="EMAIL_INGEST_VALIDATION_ERROR", message=str(exc), as_json=as_json)
            result["source"] = source_name
            result["gmail_messages"] = len(message_ids)
            results.append(result)

    rebuild_stats = None
    if rebuild_after:
        try:
            rebuild_stats = fifo_rebuild(con)
        except FifoError as exc:
            _exit_error(code="FIFO_REBUILD_ERROR", message=str(exc), as_json=as_json)

    if as_json:
        typer.echo(json.dumps(
            {"files": results, "rebuild": rebuild_stats}, ensure_ascii=False, default=str
        ))
        return
    for result in results:
        console.print(
            f"{result['source']} (账户 {result['account']}): "
            f"Gmail {result['gmail_messages']} 封，共 {result['total_rows']} 笔，"
            f"新增 {result['inserted']}，跳过重复 {result['skipped']}"
        )
    if rebuild_stats is not None:
        console.print(
            f"FIFO 已重建：重放 {rebuild_stats['transactions_replayed']} 条交易，"
            f"未平批次 {rebuild_stats['open_lots']}，已实现记录 {rebuild_stats['realized_records']}"
        )


@app.command()
def rebuild(db: DbOption = None, as_json: JsonOption = False) -> None:
    """全量重放交易,重建 lots / realized(幂等)。"""
    con = dbmod.connect(str(db) if db else None)
    try:
        stats = fifo_rebuild(con)
    except FifoError as exc:
        _exit_error(code="FIFO_REBUILD_ERROR", message=str(exc), as_json=as_json)
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
