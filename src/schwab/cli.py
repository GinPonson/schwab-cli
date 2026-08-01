"""schwab CLI 入口(Typer)。

命令一览(全部支持 --json 输出,方便 AI 消费):
    schwab import <csv...>   清洗并幂等导入嘉信交易 CSV
    schwab import-email      从 Gmail 原始邮件导入 eConfirm
    schwab reconcile         只读对账 Gmail eConfirm 与数据库
    schwab rebuild           全量重放 FIFO,重建持仓与已实现损益
    schwab positions         当前持仓(股票股数 + 期权合约、成本、方向)
    schwab expiring          已过期和即将到期的期权仓位
    schwab audit             审计账本完整性与派生表一致性
    schwab monthly           按月汇总已实现交易绩效
    schwab allocation        按历史成本分析当前仓位配置
    schwab attribution       按维度拆解已实现损益贡献
    schwab drawdown          分析累计已实现损益回撤
    schwab risk              汇总当前期权到期与集中度风险
    schwab holding-period    分析持有天数与已实现绩效
    schwab extremes          查看最佳与最差平仓交易
    schwab streaks           统计连续盈利和连续亏损
    schwab stress            模拟长期权权利金归零压力
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
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Optional

import typer
import duckdb
from rich.console import Console
from rich.table import Table

from . import db as dbmod
from .email_ingest import (
    EmailTrade,
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


def _connect_query(db: Path | None, as_json: bool):
    """为查询命令打开严格只读连接，路径错误时不创建空数据库。

    参数:
        db:      CLI 显式数据库路径；为空时沿用环境变量与默认路径。
        as_json: 是否按稳定 JSON 错误协议输出。

    返回:
        已存在 DuckDB 文件的只读连接。
    """
    try:
        return dbmod.connect_read_only(str(db) if db else None)
    except (FileNotFoundError, duckdb.Error) as exc:
        _exit_error(code="QUERY_DATABASE_ERROR", message=str(exc), as_json=as_json)


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


def _parse_optional_date(value: str | None, option: str, as_json: bool) -> date | None:
    """严格解析可选 ISO 日期，失败时按统一 CLI 错误协议退出。

    参数:
        value:   用户输入的 ``YYYY-MM-DD``，为空表示不限制。
        option:  选项名称，用于生成可定位的错误信息。
        as_json: 是否输出机器可读错误。
    """
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        _exit_error(
            code="INVALID_ARGUMENT", message=f"无法解析 {option} 日期: {value!r}",
            as_json=as_json,
        )


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


def _load_email_sources(
    con, files: list[str], explicit_account: str | None,
) -> list[tuple[str, str, list[EmailTrade], list[str]]]:
    """读取并严格解析 Gmail 输入，供导入和只读对账共同复用。

    参数:
        con:              已打开的 DuckDB 连接，用于解析账户尾号。
        files:            Gmail API JSON、原始 MIME 路径或单个 ``-``。
        explicit_account: 用户显式指定的完整账户标识。

    返回:
        ``(来源名, 账户, 成交列表, Gmail message id 列表)``。
    """
    if files.count("-") > 1:
        raise IngestError("标准输入 - 只能出现一次")
    prepared: list[tuple[str, str, list[EmailTrade], list[str]]] = []
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
        resolved_account = _resolve_email_account(con, suffixes.pop(), explicit_account)
        trades: list[EmailTrade] = []
        for message in messages:
            trades.extend(parse_econfirm(message))
        # 官方 CSV 是日期倒序；稳定排序保留同日邮件内的展示顺序。
        trades.sort(key=lambda trade: trade.trade_date, reverse=True)
        prepared.append((
            source_name, resolved_account, trades,
            [message.message_id for message in messages],
        ))
    return prepared


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
    try:
        # 先解析并校验全部输入，任一邮件异常时不导入任何文件。
        prepared = _load_email_sources(con, files, account)
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
def reconcile(
    files: Annotated[list[str], typer.Argument(
        help="用于对账的 Gmail API JSON、原始 .eml，或用 - 从 stdin 读取"
    )],
    db: DbOption = None,
    as_json: JsonOption = False,
    account: Annotated[Optional[str], typer.Option(
        "--account", help="完整账户标识；数据库尾号不唯一时必填"
    )] = None,
) -> None:
    """只读比较 Gmail eConfirm 与数据库交易，不写入或重建。"""
    try:
        con = dbmod.connect_read_only(str(db) if db else None)
    except (FileNotFoundError, duckdb.Error) as exc:
        # DuckDB 只读打开错误统一转换为稳定 CLI 协议，避免输出 traceback。
        _exit_error(code="EMAIL_RECONCILE_ERROR", message=str(exc), as_json=as_json)
    try:
        prepared = _load_email_sources(con, files, account)
    except IngestError as exc:
        _exit_error(code="EMAIL_RECONCILE_ERROR", message=str(exc), as_json=as_json)

    rows: list[dict] = []
    consumed: dict[tuple, int] = {}
    for source_name, resolved_account, trades, _ in prepared:
        for trade in trades:
            key = (
                resolved_account, trade.trade_date, trade.action, trade.symbol,
                trade.quantity, trade.price, trade.fees, trade.amount,
            )
            exact = con.execute(
                """
                SELECT txn_hash FROM transactions
                WHERE account = ? AND txn_date = ? AND action = ? AND raw_symbol = ?
                  AND quantity = ? AND price = ? AND coalesce(fees, 0) = ? AND amount = ?
                ORDER BY txn_hash
                """,
                list(key),
            ).fetchall()
            occurrence = consumed.get(key, 0)
            if occurrence < len(exact):
                status = "matched"
                txn_hash = exact[occurrence][0]
                consumed[key] = occurrence + 1
                candidates = 1
            else:
                loose = con.execute(
                    """
                    SELECT txn_hash FROM transactions
                    WHERE account = ? AND txn_date = ? AND raw_symbol = ?
                    ORDER BY txn_hash
                    """,
                    [resolved_account, trade.trade_date, trade.symbol],
                ).fetchall()
                status = "conflict" if loose else "missing"
                txn_hash = None
                candidates = len(loose)
            rows.append({
                "status": status,
                "source": source_name,
                "txn_date": trade.trade_date,
                "action": trade.action,
                "symbol": trade.symbol,
                "quantity": trade.quantity,
                "price": trade.price,
                "fees": trade.fees,
                "amount": trade.amount,
                "txn_hash": txn_hash,
                "candidates": candidates,
            })
    counts = {status: sum(row["status"] == status for row in rows)
              for status in ("matched", "missing", "conflict")}
    _emit(
        rows,
        ["status", "txn_date", "action", "symbol", "quantity", "price", "fees", "amount"],
        "Gmail / 数据库对账",
        as_json,
        extra={"count": len(rows), **counts, "read_only": True},
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
    con = _connect_query(db, as_json)
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
def expiring(
    db: DbOption = None,
    as_json: JsonOption = False,
    days: Annotated[int, typer.Option(
        "--days", "-d", help="列出未来多少天内到期的期权，同时包含已过期未平仓"
    )] = 30,
    as_of: Annotated[Optional[str], typer.Option(
        "--as-of", help="分析基准日 YYYY-MM-DD，默认今天"
    )] = None,
) -> None:
    """列出已过期、今日到期和即将到期的未平期权仓位。"""
    if days < 0:
        _exit_error(code="INVALID_ARGUMENT", message="--days 不能为负数", as_json=as_json)
    try:
        analysis_date = date.fromisoformat(as_of) if as_of else date.today()
    except ValueError:
        _exit_error(
            code="INVALID_ARGUMENT", message=f"无法解析 --as-of 日期: {as_of!r}",
            as_json=as_json,
        )
    cutoff = analysis_date + timedelta(days=days)
    con = _connect_query(db, as_json)
    rows = _query(
        con,
        """
        SELECT account, symbol_key, underlying, direction, qty, cost,
               expiry, strike, option_type, first_open_date
        FROM v_positions
        WHERE asset_type = 'option' AND expiry <= ?
        ORDER BY expiry, underlying, strike, direction
        """,
        [cutoff],
    )
    counts = {"expired": 0, "expires_today": 0, "upcoming": 0}
    for row in rows:
        remaining = (row["expiry"] - analysis_date).days
        if remaining < 0:
            status = "expired"
        elif remaining == 0:
            status = "expires_today"
        else:
            status = "upcoming"
        counts[status] += 1
        row["status"] = status
        row["days_remaining"] = remaining
        # v_positions.cost 是合约权利金报价 × 数量；换算成美元需乘标准乘数 100。
        row["premium_dollars"] = (row["cost"] * Decimal(100)).quantize(Decimal("0.01"))
    _emit(
        rows,
        ["status", "days_remaining", "symbol_key", "direction", "qty",
         "premium_dollars", "expiry", "first_open_date"],
        f"到期期权（基准日 {analysis_date}，未来 {days} 天）",
        as_json,
        extra={"count": len(rows), "as_of": analysis_date, "cutoff": cutoff, **counts},
    )


@app.command()
def audit(db: DbOption = None, as_json: JsonOption = False) -> None:
    """只读审计账本与 FIFO 派生表的一致性，不检查期权是否过期。"""
    con = _connect_query(db, as_json)

    checks = [
        {
            "check": "transactions_present",
            "severity": "error",
            "count": con.execute("SELECT count(*) FROM transactions").fetchone()[0],
            "expectation": "至少一条原始交易",
        },
        {
            "check": "invalid_trade_fields",
            "severity": "error",
            "count": con.execute(
                """
                SELECT count(*) FROM transactions
                WHERE asset_type IN ('stock', 'option')
                  AND (raw_symbol IS NULL OR underlying IS NULL OR quantity IS NULL
                       OR quantity <= 0)
                """
            ).fetchone()[0],
            "expectation": "0",
        },
        {
            "check": "unreflected_fifo_transactions",
            "severity": "error",
            "count": con.execute(
                """
                SELECT count(*) FROM transactions t
                WHERE t.asset_type IN ('stock', 'option')
                  AND NOT EXISTS (SELECT 1 FROM lots l WHERE l.open_txn = t.txn_hash)
                  AND NOT EXISTS (SELECT 1 FROM realized r WHERE r.open_txn = t.txn_hash)
                  AND NOT EXISTS (SELECT 1 FROM realized r WHERE r.close_txn = t.txn_hash)
                """
            ).fetchone()[0],
            "expectation": "0",
        },
        {
            "check": "orphan_open_lots",
            "severity": "error",
            "count": con.execute(
                """
                SELECT count(*) FROM lots l
                LEFT JOIN transactions t ON t.txn_hash = l.open_txn
                WHERE t.txn_hash IS NULL
                """
            ).fetchone()[0],
            "expectation": "0",
        },
        {
            "check": "orphan_realized_rows",
            "severity": "error",
            "count": con.execute(
                """
                SELECT count(*) FROM realized r
                LEFT JOIN transactions o ON o.txn_hash = r.open_txn
                LEFT JOIN transactions c ON c.txn_hash = r.close_txn
                WHERE o.txn_hash IS NULL OR c.txn_hash IS NULL
                """
            ).fetchone()[0],
            "expectation": "0",
        },
        {
            "check": "invalid_lot_quantities",
            "severity": "error",
            "count": con.execute(
                """
                SELECT count(*) FROM lots
                WHERE open_qty <= 0 OR remaining_qty <= 0 OR remaining_qty > open_qty
                """
            ).fetchone()[0],
            "expectation": "0",
        },
    ]
    # 第一项是存在性检查，其余检查均要求异常计数为零。
    for index, check in enumerate(checks):
        check["status"] = "pass" if (check["count"] > 0 if index == 0 else check["count"] == 0) else "fail"

    max_txn_date = con.execute("SELECT max(txn_date) FROM transactions").fetchone()[0]
    transaction_count = checks[0]["count"]
    derived_open_count = con.execute("SELECT count(*) FROM lots").fetchone()[0]
    derived_realized_count = con.execute("SELECT count(*) FROM realized").fetchone()[0]
    # 有交易但派生表均为空通常意味着导入后遗漏 rebuild；纯现金账本除外。
    trade_count = con.execute(
        "SELECT count(*) FROM transactions WHERE asset_type IN ('stock', 'option')"
    ).fetchone()[0]
    derivation_missing = int(trade_count > 0 and derived_open_count == 0 and derived_realized_count == 0)
    checks.append({
        "check": "fifo_derivatives_present",
        "severity": "error",
        "count": derivation_missing,
        "expectation": "0",
        "status": "pass" if derivation_missing == 0 else "fail",
    })
    failed = sum(check["status"] == "fail" for check in checks)
    _emit(
        checks, ["status", "severity", "check", "count", "expectation"], "账本审计",
        as_json,
        extra={
            "ok": failed == 0, "failed": failed, "transaction_count": transaction_count,
            "latest_transaction_date": max_txn_date, "read_only": True,
            "scope_note": "按用户要求，本命令不检查已过期期权",
        },
    )
    if failed:
        raise typer.Exit(code=1)


@app.command()
def monthly(
    db: DbOption = None,
    as_json: JsonOption = False,
    from_date: Annotated[Optional[str], typer.Option("--from", help="起始平仓日期 YYYY-MM-DD")] = None,
    to_date: Annotated[Optional[str], typer.Option("--to", help="截止平仓日期 YYYY-MM-DD")] = None,
) -> None:
    """按月汇总已实现损益、胜率、盈亏比与费用。"""
    start = _parse_optional_date(from_date, "--from", as_json)
    end = _parse_optional_date(to_date, "--to", as_json)
    if start and end and start > end:
        _exit_error(code="INVALID_ARGUMENT", message="--from 不能晚于 --to", as_json=as_json)
    con = _connect_query(db, as_json)
    conditions = []
    params: list = []
    if start:
        conditions.append("close_date >= ?")
        params.append(start)
    if end:
        conditions.append("close_date <= ?")
        params.append(end)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = _query(con, f"""
        SELECT strftime(close_date, '%Y-%m') AS month,
               count(*) AS closed_lots,
               count(DISTINCT close_txn) AS close_transactions,
               count_if(pnl > 0) AS wins,
               count_if(pnl < 0) AS losses,
               coalesce(sum(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0) AS gross_profit,
               coalesce(sum(CASE WHEN pnl < 0 THEN pnl ELSE 0 END), 0) AS gross_loss,
               sum(pnl) AS net_pnl,
               sum(open_fees + close_fees) AS fees
        FROM realized {where}
        GROUP BY month ORDER BY month
    """, params)
    for row in rows:
        decisive = row["wins"] + row["losses"]
        row["win_rate_pct"] = (
            (Decimal(row["wins"]) * 100 / Decimal(decisive)).quantize(Decimal("0.01"))
            if decisive else None
        )
        row["profit_factor"] = (
            (row["gross_profit"] / abs(row["gross_loss"])).quantize(Decimal("0.01"))
            if row["gross_loss"] < 0 else None
        )
    total_pnl = sum((row["net_pnl"] for row in rows), Decimal(0))
    total_fees = sum((row["fees"] for row in rows), Decimal(0))
    _emit(
        rows,
        ["month", "closed_lots", "close_transactions", "wins", "losses",
         "win_rate_pct", "gross_profit", "gross_loss", "profit_factor", "net_pnl", "fees"],
        "月度已实现绩效", as_json,
        extra={"months": len(rows), "total_pnl": total_pnl, "total_fees": total_fees,
               "basis": "realized_fifo"},
    )


@app.command()
def allocation(
    db: DbOption = None,
    as_json: JsonOption = False,
    group_by: Annotated[str, typer.Option(
        "--group-by", help="聚合维度: underlying 或 asset-type"
    )] = "underlying",
) -> None:
    """按未平仓历史成本分析配置；该结果不是实时市值配置。"""
    if group_by not in {"underlying", "asset-type"}:
        _exit_error(
            code="INVALID_ARGUMENT", message="--group-by 仅支持 underlying 或 asset-type",
            as_json=as_json,
        )
    con = _connect_query(db, as_json)
    dimension = "underlying" if group_by == "underlying" else "asset_type"
    rows = _query(con, f"""
        SELECT {dimension} AS category,
               count(*) AS positions,
               sum(CASE WHEN direction = 'long' THEN 1 ELSE 0 END) AS long_positions,
               sum(CASE WHEN direction = 'short' THEN 1 ELSE 0 END) AS short_positions,
               sum(abs(cost) * CASE WHEN asset_type = 'option' THEN 100 ELSE 1 END) AS cost_basis
        FROM v_positions
        GROUP BY {dimension}
        ORDER BY cost_basis DESC, category
    """)
    # 先用未舍入金额计算总额和占比，避免改变分组维度后产生累计分币差异。
    raw_total_cost = sum((row["cost_basis"] for row in rows), Decimal(0))
    for row in rows:
        row["allocation_pct"] = (
            (row["cost_basis"] * 100 / raw_total_cost).quantize(Decimal("0.01"))
            if raw_total_cost else None
        )
        row["cost_basis"] = row["cost_basis"].quantize(Decimal("0.01"))
    total_cost = raw_total_cost.quantize(Decimal("0.01"))
    _emit(
        rows,
        ["category", "positions", "long_positions", "short_positions", "cost_basis", "allocation_pct"],
        f"当前仓位配置（按{dimension}历史成本）", as_json,
        extra={"groups": len(rows), "total_cost_basis": total_cost,
               "basis": "historical_cost_not_market_value"},
    )


@app.command()
def attribution(
    db: DbOption = None,
    as_json: JsonOption = False,
    group_by: Annotated[str, typer.Option(
        "--group-by", help="归因维度: underlying、asset-type、direction 或 close-action"
    )] = "underlying",
    from_date: Annotated[Optional[str], typer.Option("--from", help="起始平仓日期 YYYY-MM-DD")] = None,
    to_date: Annotated[Optional[str], typer.Option("--to", help="截止平仓日期 YYYY-MM-DD")] = None,
) -> None:
    """按指定维度拆解 FIFO 已实现损益贡献。"""
    dimensions = {
        "underlying": "underlying",
        "asset-type": "asset_type",
        "direction": "direction",
        "close-action": "close_action",
    }
    if group_by not in dimensions:
        _exit_error(
            code="INVALID_ARGUMENT",
            message="--group-by 仅支持 underlying、asset-type、direction 或 close-action",
            as_json=as_json,
        )
    start = _parse_optional_date(from_date, "--from", as_json)
    end = _parse_optional_date(to_date, "--to", as_json)
    if start and end and start > end:
        _exit_error(code="INVALID_ARGUMENT", message="--from 不能晚于 --to", as_json=as_json)
    conditions = []
    params: list = []
    if start:
        conditions.append("close_date >= ?")
        params.append(start)
    if end:
        conditions.append("close_date <= ?")
        params.append(end)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    dimension = dimensions[group_by]
    con = _connect_query(db, as_json)
    rows = _query(con, f"""
        SELECT {dimension} AS category,
               count(*) AS closed_lots,
               count(DISTINCT close_txn) AS close_transactions,
               count_if(pnl > 0) AS wins,
               count_if(pnl < 0) AS losses,
               sum(pnl) AS net_pnl,
               sum(open_fees + close_fees) AS fees
        FROM realized {where}
        GROUP BY {dimension}
        ORDER BY net_pnl DESC, category
    """, params)
    total_absolute_pnl = sum((abs(row["net_pnl"]) for row in rows), Decimal(0))
    for row in rows:
        decisive = row["wins"] + row["losses"]
        row["win_rate_pct"] = (
            (Decimal(row["wins"]) * 100 / Decimal(decisive)).quantize(Decimal("0.01"))
            if decisive else None
        )
        # 使用绝对净贡献作为分母，正负贡献不会互相抵消。
        row["absolute_contribution_pct"] = (
            (abs(row["net_pnl"]) * 100 / total_absolute_pnl).quantize(Decimal("0.01"))
            if total_absolute_pnl else None
        )
    total_pnl = sum((row["net_pnl"] for row in rows), Decimal(0))
    _emit(
        rows,
        ["category", "closed_lots", "close_transactions", "wins", "losses",
         "win_rate_pct", "net_pnl", "absolute_contribution_pct", "fees"],
        f"已实现损益归因（{group_by}）", as_json,
        extra={"groups": len(rows), "total_pnl": total_pnl, "basis": "realized_fifo"},
    )


@app.command()
def drawdown(
    db: DbOption = None,
    as_json: JsonOption = False,
    from_date: Annotated[Optional[str], typer.Option("--from", help="起始平仓日期 YYYY-MM-DD")] = None,
    to_date: Annotated[Optional[str], typer.Option("--to", help="截止平仓日期 YYYY-MM-DD")] = None,
) -> None:
    """按日计算累计已实现损益及其峰谷回撤。"""
    start = _parse_optional_date(from_date, "--from", as_json)
    end = _parse_optional_date(to_date, "--to", as_json)
    if start and end and start > end:
        _exit_error(code="INVALID_ARGUMENT", message="--from 不能晚于 --to", as_json=as_json)
    conditions = []
    params: list = []
    if start:
        conditions.append("close_date >= ?")
        params.append(start)
    if end:
        conditions.append("close_date <= ?")
        params.append(end)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    con = _connect_query(db, as_json)
    daily = _query(con, f"""
        SELECT close_date AS date, sum(pnl) AS daily_pnl
        FROM realized {where}
        GROUP BY close_date ORDER BY close_date
    """, params)

    cumulative = Decimal(0)
    peak = Decimal(0)
    peak_date: date | None = None
    max_drawdown = Decimal(0)
    max_peak_date: date | None = None
    trough_date: date | None = None
    recovery_date: date | None = None
    active_peak = Decimal(0)
    rows: list[dict] = []
    for row in daily:
        cumulative += row["daily_pnl"]
        if cumulative > peak:
            peak = cumulative
            peak_date = row["date"]
        current_drawdown = peak - cumulative
        if current_drawdown > max_drawdown:
            max_drawdown = current_drawdown
            max_peak_date = peak_date
            trough_date = row["date"]
            recovery_date = None
            active_peak = peak
        elif trough_date and recovery_date is None and row["date"] > trough_date and cumulative >= active_peak:
            recovery_date = row["date"]
        rows.append({
            "date": row["date"], "daily_pnl": row["daily_pnl"],
            "cumulative_pnl": cumulative, "running_peak": peak,
            "drawdown": current_drawdown,
        })
    _emit(
        rows, ["date", "daily_pnl", "cumulative_pnl", "running_peak", "drawdown"],
        "累计已实现损益回撤", as_json,
        extra={
            "days": len(rows), "ending_cumulative_pnl": cumulative,
            "max_drawdown": max_drawdown, "peak_date": max_peak_date,
            "trough_date": trough_date, "recovery_date": recovery_date,
            "basis": "daily_realized_fifo",
        },
    )


@app.command()
def risk(
    db: DbOption = None,
    as_json: JsonOption = False,
    as_of: Annotated[Optional[str], typer.Option(
        "--as-of", help="分析基准日 YYYY-MM-DD，默认今天"
    )] = None,
) -> None:
    """按到期窗口分别汇总长期权成本与短期权收入。"""
    analysis_date = _parse_optional_date(as_of, "--as-of", as_json) or date.today()
    con = _connect_query(db, as_json)
    positions = _query(con, """
        SELECT underlying, symbol_key, direction, qty, cost, expiry
        FROM v_positions
        WHERE asset_type = 'option'
        ORDER BY expiry, underlying, symbol_key
    """)
    bucket_order = {"expired": 0, "0-30d": 1, "31-60d": 2, "61-90d": 3, "90d+": 4}
    buckets: dict[str, dict] = {}
    long_by_underlying: dict[str, Decimal] = {}
    total_long_paid = Decimal(0)
    total_short_received = Decimal(0)
    for position in positions:
        days_remaining = (position["expiry"] - analysis_date).days
        if days_remaining < 0:
            bucket = "expired"
        elif days_remaining <= 30:
            bucket = "0-30d"
        elif days_remaining <= 60:
            bucket = "31-60d"
        elif days_remaining <= 90:
            bucket = "61-90d"
        else:
            bucket = "90d+"
        premium = (abs(position["cost"]) * Decimal(100)).quantize(Decimal("0.01"))
        item = buckets.setdefault(bucket, {
            "bucket": bucket, "positions": 0, "contracts": Decimal(0),
            "long_positions": 0, "short_positions": 0,
            "long_contracts": Decimal(0), "short_contracts": Decimal(0),
            "long_premium_paid": Decimal(0), "short_premium_received": Decimal(0),
        })
        item["positions"] += 1
        item["contracts"] += position["qty"]
        item[f"{position['direction']}_positions"] += 1
        item[f"{position['direction']}_contracts"] += position["qty"]
        if position["direction"] == "long":
            item["long_premium_paid"] += premium
            long_by_underlying[position["underlying"]] = (
                long_by_underlying.get(position["underlying"], Decimal(0)) + premium
            )
            total_long_paid += premium
        else:
            item["short_premium_received"] += premium
            total_short_received += premium
    rows = sorted(buckets.values(), key=lambda row: bucket_order[row["bucket"]])
    for row in rows:
        row["long_premium_paid"] = row["long_premium_paid"].quantize(Decimal("0.01"))
        row["short_premium_received"] = row["short_premium_received"].quantize(
            Decimal("0.01")
        )
        row["long_premium_pct"] = (
            (row["long_premium_paid"] * 100 / total_long_paid).quantize(Decimal("0.01"))
            if total_long_paid else None
        )
    ranked = sorted(long_by_underlying.items(), key=lambda item: (-item[1], item[0]))
    top_three_cost = sum((cost for _, cost in ranked[:3]), Decimal(0))
    top_long_underlyings = [
        {
            "underlying": underlying,
            "long_premium_paid": cost,
            "long_premium_pct": (
                (cost * 100 / total_long_paid).quantize(Decimal("0.01"))
                if total_long_paid else None
            ),
        }
        for underlying, cost in ranked[:3]
    ]
    _emit(
        rows,
        ["bucket", "positions", "contracts", "long_positions", "short_positions",
         "long_contracts", "short_contracts", "long_premium_paid",
         "short_premium_received", "long_premium_pct"],
        f"当前期权风险日历（基准日 {analysis_date}）", as_json,
        extra={
            "as_of": analysis_date, "option_positions": len(positions),
            "total_long_premium_paid": total_long_paid,
            "total_short_premium_received": total_short_received,
            "top_three_long_underlyings": top_long_underlyings,
            "top_three_long_concentration_pct": (
                (top_three_cost * 100 / total_long_paid).quantize(Decimal("0.01"))
                if total_long_paid else None
            ),
            "basis": "long_paid_and_short_received_premium_not_market_risk",
        },
    )


@app.command(name="holding-period")
def holding_period(
    db: DbOption = None,
    as_json: JsonOption = False,
) -> None:
    """按 FIFO lot 的持有天数区间汇总已实现绩效。"""
    con = _connect_query(db, as_json)
    rows = _query(con, """
        WITH classified AS (
            SELECT *, date_diff('day', open_date, close_date) AS holding_days,
                   CASE
                       WHEN date_diff('day', open_date, close_date) <= 7 THEN '0-7d'
                       WHEN date_diff('day', open_date, close_date) <= 30 THEN '8-30d'
                       WHEN date_diff('day', open_date, close_date) <= 90 THEN '31-90d'
                       ELSE '90d+'
                   END AS bucket,
                   CASE
                       WHEN date_diff('day', open_date, close_date) <= 7 THEN 1
                       WHEN date_diff('day', open_date, close_date) <= 30 THEN 2
                       WHEN date_diff('day', open_date, close_date) <= 90 THEN 3
                       ELSE 4
                   END AS bucket_order
            FROM realized
        )
        SELECT bucket, min(bucket_order) AS bucket_order,
               count(*) AS closed_lots,
               count(DISTINCT close_txn) AS close_transactions,
               count_if(pnl > 0) AS wins,
               count_if(pnl < 0) AS losses,
               round(avg(holding_days), 2) AS average_holding_days,
               sum(pnl) AS net_pnl,
               round(avg(pnl), 4) AS average_lot_pnl
        FROM classified
        GROUP BY bucket
        ORDER BY bucket_order
    """)
    for row in rows:
        decisive = row["wins"] + row["losses"]
        row["win_rate_pct"] = (
            (Decimal(row["wins"]) * 100 / Decimal(decisive)).quantize(Decimal("0.01"))
            if decisive else None
        )
        del row["bucket_order"]
    _emit(
        rows,
        ["bucket", "closed_lots", "close_transactions", "wins", "losses",
         "win_rate_pct", "average_holding_days", "average_lot_pnl", "net_pnl"],
        "持有周期绩效（FIFO lot）", as_json,
        extra={"buckets": len(rows), "basis": "realized_fifo_lots"},
    )


def _closed_transaction_query() -> str:
    """返回把多个 FIFO realized 行合并回一笔平仓交易的公共查询。"""
    return """
        SELECT close_txn, account, close_date, symbol_key, underlying, asset_type,
               direction, close_action, sum(qty) AS qty, sum(pnl) AS pnl,
               sum(open_fees + close_fees) AS fees,
               min(open_date) AS earliest_open_date,
               max(open_date) AS latest_open_date
        FROM realized
        GROUP BY close_txn, account, close_date, symbol_key, underlying,
                 asset_type, direction, close_action
    """


@app.command()
def extremes(
    db: DbOption = None,
    as_json: JsonOption = False,
    limit: Annotated[int, typer.Option("--limit", "-n", help="最佳和最差各返回多少笔")] = 10,
) -> None:
    """按平仓交易聚合 FIFO lot，列出最佳和最差交易。"""
    if limit <= 0:
        _exit_error(code="INVALID_ARGUMENT", message="--limit 必须大于 0", as_json=as_json)
    con = _connect_query(db, as_json)
    base = _closed_transaction_query()
    best = _query(con, f"SELECT * FROM ({base}) ORDER BY pnl DESC, close_date, close_txn LIMIT ?", [limit])
    worst = _query(con, f"SELECT * FROM ({base}) ORDER BY pnl ASC, close_date, close_txn LIMIT ?", [limit])
    rows = []
    for rank, row in enumerate(best, start=1):
        rows.append({"side": "best", "rank": rank, **row})
    for rank, row in enumerate(worst, start=1):
        rows.append({"side": "worst", "rank": rank, **row})
    _emit(
        rows,
        ["side", "rank", "close_date", "underlying", "symbol_key", "asset_type",
         "direction", "close_action", "qty", "pnl", "fees"],
        "最佳与最差平仓交易", as_json,
        extra={"per_side": limit, "basis": "aggregated_close_transaction"},
    )


@app.command()
def streaks(db: DbOption = None, as_json: JsonOption = False) -> None:
    """按平仓交易顺序统计最长连续盈利和连续亏损。"""
    con = _connect_query(db, as_json)
    trades = _query(con, f"""
        SELECT * FROM ({_closed_transaction_query()})
        ORDER BY close_date, close_txn
    """)
    longest: dict[str, dict | None] = {"win": None, "loss": None}
    current_kind: str | None = None
    current: dict | None = None
    neutral_trades = 0

    def finish_streak() -> None:
        """把当前连续区间写入对应类别的最长记录。"""
        nonlocal current
        if current is None or current_kind is None:
            return
        previous = longest[current_kind]
        if previous is None or current["count"] > previous["count"]:
            longest[current_kind] = current.copy()

    for trade in trades:
        kind = "win" if trade["pnl"] > 0 else "loss" if trade["pnl"] < 0 else None
        if kind is None:
            neutral_trades += 1
            finish_streak()
            current_kind = None
            current = None
            continue
        if kind != current_kind:
            finish_streak()
            current_kind = kind
            current = {
                "kind": kind, "count": 1, "start_date": trade["close_date"],
                "end_date": trade["close_date"], "pnl": trade["pnl"],
            }
        else:
            current["count"] += 1
            current["end_date"] = trade["close_date"]
            current["pnl"] += trade["pnl"]
    finish_streak()
    rows = [row for row in (longest["win"], longest["loss"]) if row is not None]
    _emit(
        rows, ["kind", "count", "start_date", "end_date", "pnl"],
        "最长连续盈亏", as_json,
        extra={"close_transactions": len(trades), "neutral_transactions": neutral_trades,
               "basis": "aggregated_close_transaction"},
    )


@app.command()
def stress(db: DbOption = None, as_json: JsonOption = False) -> None:
    """模拟当前长期权按标的或全部归零时的最大权利金损失。"""
    con = _connect_query(db, as_json)
    rows = _query(con, """
        SELECT underlying, sum(qty) AS contracts,
               sum(cost * 100) AS premium_at_risk,
               min(expiry) AS nearest_expiry,
               max(expiry) AS farthest_expiry
        FROM v_positions
        WHERE asset_type = 'option' AND direction = 'long'
        GROUP BY underlying
        ORDER BY premium_at_risk DESC, underlying
    """)
    total = sum((row["premium_at_risk"] for row in rows), Decimal(0)).quantize(
        Decimal("0.01")
    )
    for row in rows:
        row["premium_at_risk"] = row["premium_at_risk"].quantize(Decimal("0.01"))
        row["loss_if_zero"] = -row["premium_at_risk"]
        row["portfolio_premium_pct"] = (
            (row["premium_at_risk"] * 100 / total).quantize(Decimal("0.01"))
            if total else None
        )
    top_three = sum((row["premium_at_risk"] for row in rows[:3]), Decimal(0)).quantize(
        Decimal("0.01")
    )
    _emit(
        rows,
        ["underlying", "contracts", "premium_at_risk", "loss_if_zero",
         "portfolio_premium_pct", "nearest_expiry", "farthest_expiry"],
        "长期权权利金归零压力测试", as_json,
        extra={
            "long_option_contracts": sum((row["contracts"] for row in rows), Decimal(0)),
            "all_long_options_loss_if_zero": -total,
            "top_three_loss_if_zero": -top_three,
            "basis": "maximum_paid_premium_loss_no_market_data",
        },
    )


@app.command()
def realized(
    db: DbOption = None,
    as_json: JsonOption = False,
    symbol: Annotated[Optional[str], typer.Option("--symbol", "-s", help="按标的/合约过滤")] = None,
    from_date: Annotated[Optional[str], typer.Option("--from", help="起始日期 YYYY-MM-DD")] = None,
    to_date: Annotated[Optional[str], typer.Option("--to", help="截止日期 YYYY-MM-DD")] = None,
) -> None:
    """已实现损益明细(含合计)。"""
    start = _parse_optional_date(from_date, "--from", as_json)
    end = _parse_optional_date(to_date, "--to", as_json)
    if start and end and start > end:
        _exit_error(code="INVALID_ARGUMENT", message="--from 不能晚于 --to", as_json=as_json)
    con = _connect_query(db, as_json)
    sql = "SELECT * FROM realized WHERE 1=1"
    params: list = []
    if symbol:
        sql += " AND (underlying = ? OR symbol_key = ?)"
        params += [symbol.upper(), symbol.upper()]
    if start:
        sql += " AND close_date >= ?"
        params.append(start)
    if end:
        sql += " AND close_date <= ?"
        params.append(end)
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
    start = _parse_optional_date(from_date, "--from", as_json)
    end = _parse_optional_date(to_date, "--to", as_json)
    if start and end and start > end:
        _exit_error(code="INVALID_ARGUMENT", message="--from 不能晚于 --to", as_json=as_json)
    if limit <= 0:
        _exit_error(code="INVALID_ARGUMENT", message="--limit 必须大于 0", as_json=as_json)
    con = _connect_query(db, as_json)
    sql = "SELECT * FROM transactions WHERE 1=1"
    params: list = []
    if symbol:
        sql += " AND (underlying = ? OR raw_symbol = ?)"
        params += [symbol.upper(), symbol.upper()]
    if action:
        sql += " AND action = ?"
        params.append(action)
    if start:
        sql += " AND txn_date >= ?"
        params.append(start)
    if end:
        sql += " AND txn_date <= ?"
        params.append(end)
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
    start = _parse_optional_date(from_date, "--from", as_json)
    end = _parse_optional_date(to_date, "--to", as_json)
    if start and end and start > end:
        _exit_error(code="INVALID_ARGUMENT", message="--from 不能晚于 --to", as_json=as_json)
    con = _connect_query(db, as_json)
    sql = "SELECT * FROM v_cashflows WHERE 1=1"
    params: list = []
    if start:
        sql += " AND txn_date >= ?"
        params.append(start)
    if end:
        sql += " AND txn_date <= ?"
        params.append(end)
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
    con = _connect_query(db, as_json)

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
