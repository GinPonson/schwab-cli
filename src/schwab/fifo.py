"""FIFO 重放引擎:由 transactions 表全量重建 lots(未平批次)与 realized(已实现损益)。

嘉信语义:
- 股票:Buy 开 long,Sell 按 FIFO 消耗 long
- 期权(按完整合约名独立成簿,乘数 100):
  Buy to Open 开 long,Sell to Open 开 short;
  Sell to Close 平 long,Buy to Close 平 short;
  Expired 按 $0 平仓(权利方亏光权利金 / 义务方落袋权利金);
  Assigned 按 $0 平 short(权利金已收;被行权的股票交割在 CSV 中是
  独立的 Buy/Sell 行,随股票簿走普通 FIFO)

重放顺序:txn_date 升序,同日按 seq 降序(CSV 为最新交易在前,
同日内物理位置靠后(seq 大)的行发生得更早)。

费用处理:开仓/平仓费用都按平仓数量比例分摊进 realized,
批次完全耗尽时分摊剩余尾差,避免累计舍入漂移。

任何不一致(超卖、平仓方向无持仓等)抛 FifoError 中止,不猜测、不兜底。
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace
from typing import Deque

from .ingest import OPTION_MULTIPLIER


class FifoError(Exception):
    """重放过程中发现数据不一致(超卖、无持仓可平等)时抛出。"""


# 开仓 Action -> 持仓方向
_OPEN_ACTIONS = {
    "Buy": "long",
    "Buy to Open": "long",
    "Sell to Open": "short",
}

# 平仓 Action -> 被平的持仓方向(Expired 特殊:方向由现有持仓决定)
_CLOSE_ACTIONS = {
    "Sell": "long",
    "Sell to Close": "long",
    "Buy to Close": "short",
    "Assigned": "short",
}


@dataclass
class Lot:
    """一个未平批次(FIFO 队列元素),字段与 lots 表对应。"""

    account: str
    symbol_key: str
    asset_type: str
    underlying: str
    expiry: object
    strike: Decimal | None
    option_type: str | None
    direction: str
    open_date: object
    open_price: Decimal
    open_qty: Decimal
    remaining_qty: Decimal
    open_fees: Decimal
    open_txn: str
    fees_allocated: Decimal = field(default=Decimal(0))  # 已分摊进 realized 的开仓费


def _multiplier(asset_type: str) -> int:
    """合约乘数:期权 100,股票 1。"""
    return OPTION_MULTIPLIER if asset_type == "option" else 1


def _open_lot(txn, direction: str) -> Lot:
    """由一条开仓交易构造批次。"""
    return Lot(
        account=txn.account,
        symbol_key=txn.raw_symbol,
        asset_type=txn.asset_type,
        underlying=txn.underlying,
        expiry=txn.expiry,
        strike=txn.strike,
        option_type=txn.option_type,
        direction=direction,
        open_date=txn.txn_date,
        open_price=txn.price,
        open_qty=txn.quantity,
        remaining_qty=txn.quantity,
        open_fees=txn.fees or Decimal(0),
        open_txn=txn.txn_hash,
    )


def _consume(book: Deque[Lot], qty: Decimal, where: str) -> list[tuple[Lot, Decimal]]:
    """从 FIFO 队列消耗指定数量,返回 [(批次, 消耗数量)],不足则报错。"""
    available = sum(lot.remaining_qty for lot in book)
    if available < qty:
        raise FifoError(
            f"{where}: 平仓数量 {qty} 超过未平数量 {available}"
            f"(合约 {book[0].symbol_key if book else '未知'},方向 {book[0].direction if book else '?'})"
        )
    consumed: list[tuple[Lot, Decimal]] = []
    left = qty
    while left > 0:
        lot = book[0]
        take = min(lot.remaining_qty, left)
        lot.remaining_qty -= take
        left -= take
        consumed.append((lot, take))
        if lot.remaining_qty == 0:
            book.popleft()
    return consumed


def _alloc_open_fee(lot: Lot, take: Decimal) -> Decimal:
    """按消耗比例分摊开仓费;批次耗尽时把尾差一次性分完,保证总账对齐。"""
    if lot.remaining_qty == 0:
        alloc = lot.open_fees - lot.fees_allocated
    else:
        alloc = lot.open_fees * take / lot.open_qty
    lot.fees_allocated += alloc
    return alloc


def _close_position(txn, direction: str, close_price: Decimal, books, realized: list) -> None:
    """按 FIFO 平掉一笔持仓,生成 realized 明细(一次平仓可能跨多个批次)。"""
    key = (txn.account, txn.raw_symbol)
    book = books[key]
    where = f"{txn.txn_date} {txn.action} {txn.raw_symbol}"
    if not book or all(lot.direction != direction for lot in book):
        raise FifoError(f"{where}: 没有可平的 {direction} 持仓")

    # 平仓费用按各批次消耗数量比例分摊;最后一条吃掉尾差
    close_fees_total = txn.fees or Decimal(0)
    close_fees_allocated = Decimal(0)
    mult = _multiplier(txn.asset_type)
    # 注:books 按 (account, symbol) 成簿,同一合约不会同时持有 long 与 short
    # (嘉信不支持同合约双向持仓,Expired 的方向判定依赖此假设)
    consumed = _consume(book, txn.quantity, where)

    for idx, (lot, take) in enumerate(consumed):
        open_fee = _alloc_open_fee(lot, take)
        if idx < len(consumed) - 1:
            close_fee = close_fees_total * take / txn.quantity
            close_fees_allocated += close_fee
        else:
            close_fee = close_fees_total - close_fees_allocated
        if lot.direction == "long":
            gross = (close_price - lot.open_price) * take * mult
        else:
            gross = (lot.open_price - close_price) * take * mult
        realized.append({
            "account": lot.account,
            "symbol_key": lot.symbol_key,
            "asset_type": lot.asset_type,
            "underlying": lot.underlying,
            "direction": lot.direction,
            "open_date": lot.open_date,
            "close_date": txn.txn_date,
            "qty": take,
            "open_price": lot.open_price,
            "close_price": close_price,
            "open_fees": open_fee,
            "close_fees": close_fee,
            "pnl": (gross - open_fee - close_fee).quantize(Decimal("0.0001")),
            "close_action": txn.action,
            "open_txn": lot.open_txn,
            "close_txn": txn.txn_hash,
        })


def rebuild(con) -> dict:
    """全量重放 transactions,重建 lots 与 realized 两表,返回统计与警告。

    幂等:先清空两张产物表再重放,任意次执行结果一致。
    """
    # 同一账户、日期、合约若来自多个未对齐快照，文件内 seq 不可直接比较。
    # 此时拒绝猜测顺序，要求用户导入包含该日全部交易的较新快照重新锚定。
    ambiguous = con.execute(
        """
        SELECT account, txn_date, raw_symbol, count(DISTINCT source_hash) AS sources
        FROM transactions
        WHERE asset_type IN ('stock', 'option')
        GROUP BY account, txn_date, raw_symbol
        HAVING count(DISTINCT source_hash) > 1
        ORDER BY account, txn_date, raw_symbol
        """
    ).fetchone()
    if ambiguous is not None:
        account, txn_date, symbol, sources = ambiguous
        raise FifoError(
            f"{txn_date} {symbol}: 同日交易来自 {sources} 个未对齐的 CSV 快照"
            f"(账户 {account})，无法可靠确定 FIFO 顺序；请导入覆盖该日全部交易的最新导出文件"
        )

    # 按生效日升序、同日 seq 降序；txn_hash 仅作为完全确定性的最终排序键。
    rows = con.execute(
        """
        SELECT txn_hash, account, txn_date, action, raw_symbol, asset_type,
               underlying, expiry, strike, option_type, quantity, price, fees
        FROM transactions
        WHERE asset_type IN ('stock', 'option')
        ORDER BY txn_date ASC, seq DESC, txn_hash ASC
        """
    ).fetchall()
    cols = ["txn_hash", "account", "txn_date", "action", "raw_symbol", "asset_type",
            "underlying", "expiry", "strike", "option_type", "quantity", "price", "fees"]

    books: dict[tuple[str, str], Deque[Lot]] = defaultdict(deque)
    realized: list[dict] = []

    for row in rows:
        txn = SimpleNamespace(**dict(zip(cols, row)))  # 轻量行对象,属性访问
        key = (txn.account, txn.raw_symbol)

        if txn.action in _OPEN_ACTIONS:
            books[key].append(_open_lot(txn, _OPEN_ACTIONS[txn.action]))
        elif txn.action in _CLOSE_ACTIONS:
            # Assigned 行无成交价,被行权时期权腿按 $0 平仓(权利金已在开仓时收取)
            close_price = txn.price if txn.price is not None else Decimal(0)
            _close_position(txn, _CLOSE_ACTIONS[txn.action], close_price, books, realized)
        elif txn.action == "Expired":
            # 到期作废:方向由现有持仓决定(long 亏光 / short 落袋),按 $0 平
            book = books[key]
            directions = {lot.direction for lot in book if lot.remaining_qty > 0}
            if len(directions) != 1:
                raise FifoError(
                    f"{txn.txn_date} Expired {txn.raw_symbol}: "
                    f"无法确定持仓方向(现有方向: {directions or '无持仓'})"
                )
            _close_position(txn, directions.pop(), Decimal(0), books, realized)
        else:
            raise FifoError(f"未处理的持仓 Action: {txn.action!r}")  # 理论上 ingest 已拦截

    open_lots = [lot for book in books.values() for lot in book if lot.remaining_qty > 0]
    con.execute("BEGIN TRANSACTION")
    try:
        # 删除旧产物与写入新产物必须原子完成，避免失败后留下空表或半表。
        con.execute("DELETE FROM lots")
        con.execute("DELETE FROM realized")
        for lot in open_lots:
            con.execute(
                """
                INSERT INTO lots (account, symbol_key, asset_type, underlying, expiry,
                                  strike, option_type, direction, open_date, open_price,
                                  open_qty, remaining_qty, open_fees, open_txn)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [lot.account, lot.symbol_key, lot.asset_type, lot.underlying, lot.expiry,
                 lot.strike, lot.option_type, lot.direction, lot.open_date, lot.open_price,
                 lot.open_qty, lot.remaining_qty, lot.open_fees, lot.open_txn],
            )
        for r in realized:
            con.execute(
                """
                INSERT INTO realized (account, symbol_key, asset_type, underlying, direction,
                                      open_date, close_date, qty, open_price, close_price,
                                      open_fees, close_fees, pnl, close_action, open_txn, close_txn)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [r["account"], r["symbol_key"], r["asset_type"], r["underlying"], r["direction"],
                 r["open_date"], r["close_date"], r["qty"], r["open_price"], r["close_price"],
                 r["open_fees"], r["close_fees"], r["pnl"], r["close_action"],
                 r["open_txn"], r["close_txn"]],
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    # 数据质量检查:已过到期日仍未平的期权,通常意味着缺少 Expired/平仓行
    warnings = [
        f"期权 {lot.symbol_key} 已于 {lot.expiry} 到期,仍有 {lot.remaining_qty} 张未平"
        for lot in open_lots
        if lot.asset_type == "option" and lot.expiry is not None and lot.expiry < _max_txn_date(con)
    ]

    return {
        "transactions_replayed": len(rows),
        "open_lots": len(open_lots),
        "realized_records": len(realized),
        "total_realized_pnl": str(sum(r["pnl"] for r in realized).quantize(Decimal("0.0001"))),
        "warnings": warnings,
    }


def _max_txn_date(con):
    """全表最大生效日,用于判断"到期仍持有"是否为数据缺失。"""
    return con.execute("SELECT max(txn_date) FROM transactions").fetchone()[0]
