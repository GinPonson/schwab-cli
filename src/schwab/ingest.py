"""嘉信交易 CSV 的清洗、解析与幂等导入。

输入:嘉信网页端导出的 Transactions CSV,列为
    Date, Action, Symbol, Description, Quantity, Price, Fees & Comm, Amount

清洗规则:
- 金额:"$1,234.56" / "-$1,865.66" -> Decimal;空串 -> None
- 日期:"MM/DD/YYYY";若为 "MM/DD/YYYY as of MM/DD/YYYY",
  生效日 txn_date 取 as-of 之后的日期,record_date 保留前一个
- 期权 Symbol:"GOOG 03/19/2027 385.00 C" -> 标的/到期日/行权价/CP,乘数 100
- 勾稽校验:有价格与金额的交易行,必须满足
  |amount| == qty * price * mult - fees (误差 <= 0.011),符号与买卖方向一致
- 未知 Action、金额异常、格式异常一律抛 IngestError 中止,不静默跳过

幂等:txn_hash = 规范化行内容(含文件内行号 seq)的 sha256,
导入时 INSERT OR IGNORE,重复行自动跳过。
"""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

# 期权乘数(美股标准:1 张合约 = 100 股)
OPTION_MULTIPLIER = 100

# 持仓类 Action:参与 FIFO 重放
POSITION_ACTIONS = frozenset({
    "Buy", "Sell",
    "Buy to Open", "Sell to Open", "Buy to Close", "Sell to Close",
    "Expired", "Assigned",
})

# 现金类 Action:仅记录资金流水,不影响持仓
CASH_ACTIONS = frozenset({
    "MoneyLink Transfer", "Credit Interest",
    "Qualified Dividend", "Non-Qualified Div", "Pr Yr Non-Qual Div",
    "NRA Tax Adj", "Pr Yr NRA Tax", "Foreign Tax Paid",
})

KNOWN_ACTIONS = POSITION_ACTIONS | CASH_ACTIONS

# 期权 Symbol 形如 "GOOG 03/19/2027 385.00 C"(标的可能含点号,如 BRK.B)
OPTION_SYMBOL_RE = re.compile(r"^(\S+) (\d{2}/\d{2}/\d{4}) ([0-9]+(?:\.[0-9]+)?) ([CP])$")

# 从导出文件名解析账户,如 Individual_XXX276_Transactions_20260730-084033.csv -> XXX276
ACCOUNT_RE = re.compile(r"Individual_([^_]+)_")

# 金额勾稽允许的最大误差(美元)
AMOUNT_TOLERANCE = Decimal("0.011")

# 买入方向 Action(amount 应为负),其余持仓交易 amount 为正(Expired/Assigned 无金额)
_BUY_ACTIONS = frozenset({"Buy", "Buy to Open", "Buy to Close"})


class IngestError(Exception):
    """CSV 清洗/校验失败时抛出,携带文件名与行号定位。"""


@dataclass
class ParsedTxn:
    """一行清洗后的交易记录,字段与 transactions 表一一对应。"""

    account: str
    record_date: date
    txn_date: date
    action: str
    raw_symbol: str | None
    description: str | None
    asset_type: str  # stock | option | cash
    underlying: str | None
    expiry: date | None
    strike: Decimal | None
    option_type: str | None
    quantity: Decimal | None
    price: Decimal | None
    fees: Decimal | None
    amount: Decimal | None
    source_hash: str
    seq: int

    @property
    def txn_hash(self) -> str:
        """规范化行内容哈希(含 seq),同一文件重复导入结果一致,行级幂等。"""
        parts = [
            self.account, str(self.record_date), str(self.txn_date), self.action,
            self.raw_symbol or "", self.description or "",
            str(self.quantity or ""), str(self.price or ""),
            str(self.fees or ""), str(self.amount or ""), str(self.seq),
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()


def parse_money(raw: str) -> Decimal | None:
    """解析嘉信金额格式:"$1,234.56" / "-$1,865.66";空串返回 None。"""
    text = raw.strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        raise IngestError(f"无法解析金额: {raw!r}")


def parse_date_field(raw: str) -> tuple[date, date]:
    """解析 Date 列,返回 (record_date, txn_date)。

    支持 "MM/DD/YYYY" 与 "MM/DD/YYYY as of MM/DD/YYYY" 两种形式;
    as-of 形式下生效日为后一个日期。
    """
    text = raw.strip()
    if " as of " in text:
        record_raw, effective_raw = text.split(" as of ")
    else:
        record_raw = effective_raw = text
    try:
        record = _parse_us_date(record_raw)
        effective = _parse_us_date(effective_raw)
    except (ValueError, TypeError):
        raise IngestError(f"无法解析日期: {raw!r}")
    return record, effective


def _parse_us_date(text: str) -> date:
    """解析 "MM/DD/YYYY" 为 date。"""
    month, day, year = (int(p) for p in text.split("/"))
    return date(year, month, day)


def parse_option_symbol(symbol: str) -> tuple[str, date, Decimal, str] | None:
    """尝试按期权合约解析 Symbol,命中返回 (标的, 到期日, 行权价, C/P),否则 None。"""
    m = OPTION_SYMBOL_RE.match(symbol.strip())
    if not m:
        return None
    underlying, expiry_raw, strike_raw, cp = m.groups()
    month, day, year = (int(p) for p in expiry_raw.split("/"))
    return underlying, date(year, month, day), Decimal(strike_raw), cp


def parse_account(file_name: str) -> str:
    """从导出文件名提取账户标识,提取失败视为文件来源不明,直接报错。"""
    m = ACCOUNT_RE.search(file_name)
    if not m:
        raise IngestError(f"无法从文件名解析账户: {file_name!r}(期望形如 Individual_XXX276_...)")
    return m.group(1)


def file_digest(path: Path) -> str:
    """整个文件的 sha256,用于 import_files 登记与重复文件提示。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reconcile_amount(
    action: str, quantity: Decimal, price: Decimal, fees: Decimal,
    amount: Decimal, multiplier: int, where: str,
) -> None:
    """勾稽校验:成交金额必须与 数量*价格*乘数-费用 一致(含符号)。

    买入类 amount 为负且费用叠加(付权利金+费用),
    卖出类 amount 为正且费用扣减(收权利金-费用)。
    校验失败说明 CSV 格式超出已知规则,抛错中止而不是带病入库。
    """
    gross = quantity * price * multiplier
    if action in _BUY_ACTIONS:
        expected = -(gross + fees)
    else:
        expected = gross - fees
    if abs(amount - expected) > AMOUNT_TOLERANCE:
        raise IngestError(
            f"{where}: 金额勾稽不符 action={action!r} "
            f"qty={quantity} price={price} fees={fees} amount={amount} "
            f"(期望 {expected})"
        )


def parse_row(fields: dict[str, str], *, account: str, source_hash: str, seq: int, where: str) -> ParsedTxn:
    """把一行原始 CSV 字典清洗为 ParsedTxn,完成全部格式与勾稽校验。"""
    action = fields["Action"].strip()
    if action not in KNOWN_ACTIONS:
        raise IngestError(f"{where}: 未知 Action {action!r},请扩展 KNOWN_ACTIONS 后再导入")

    record_date, txn_date = parse_date_field(fields["Date"])
    raw_symbol = fields["Symbol"].strip() or None
    description = fields["Description"].strip() or None
    quantity = parse_money(fields["Quantity"])
    price = parse_money(fields["Price"])
    fees = parse_money(fields["Fees & Comm"])
    amount = parse_money(fields["Amount"])

    if action in CASH_ACTIONS:
        asset_type, underlying, expiry, strike, option_type = "cash", None, None, None, None
    else:
        if quantity is None:
            raise IngestError(f"{where}: 持仓类交易缺少数量 action={action!r}")
        option = parse_option_symbol(raw_symbol or "")
        if option:
            asset_type = "option"
            underlying, expiry, strike, option_type = option
            multiplier = OPTION_MULTIPLIER
        else:
            asset_type = "option" if " to " in action or action in ("Expired", "Assigned") else "stock"
            # Buy/Sell 必为股票;期权专属 Action 却解析不出合约格式 -> 数据异常
            if asset_type == "option":
                raise IngestError(f"{where}: 期权 Symbol 格式无法解析: {raw_symbol!r}")
            underlying, expiry, strike, option_type = raw_symbol, None, None, None
            multiplier = 1
        # Expired/Assigned 行无价格与金额,跳过勾稽
        if action not in ("Expired", "Assigned"):
            if price is None or amount is None:
                raise IngestError(f"{where}: 交易行缺少价格或金额 action={action!r}")
            _reconcile_amount(action, quantity, price, fees or Decimal(0), amount, multiplier, where)

    return ParsedTxn(
        account=account,
        record_date=record_date,
        txn_date=txn_date,
        action=action,
        raw_symbol=raw_symbol,
        description=description,
        asset_type=asset_type,
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        quantity=quantity,
        price=price,
        fees=fees,
        amount=amount,
        source_hash=source_hash,
        seq=seq,
    )


def parse_csv(path: Path) -> tuple[str, str, list[ParsedTxn]]:
    """解析整个 CSV 文件,返回 (账户, 文件哈希, 交易列表)。

    任一行校验失败都会抛出 IngestError,整个文件不入库(避免半导入状态)。
    """
    account = parse_account(path.name)
    digest = file_digest(path)
    txns: list[ParsedTxn] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        expected = ["Date", "Action", "Symbol", "Description", "Quantity", "Price", "Fees & Comm", "Amount"]
        if reader.fieldnames != expected:
            raise IngestError(f"{path.name}: 表头不符,期望 {expected},实际 {reader.fieldnames}")
        # seq 从 1 开始(数据首行),保持与文件中物理行序一致
        for seq, row in enumerate(reader, start=1):
            where = f"{path.name} 第{seq}行"
            txns.append(parse_row(row, account=account, source_hash=digest, seq=seq, where=where))
    return account, digest, txns


def import_csv(con, path: Path) -> dict:
    """把单个 CSV 导入 transactions 表,返回统计信息。

    行级幂等:已存在的 txn_hash 自动跳过;文件级登记 import_files 仅在
    有新增行时更新。
    """
    account, digest, txns = parse_csv(path)
    inserted = 0
    by_action: dict[str, int] = {}
    for txn in txns:
        # INSERT OR IGNORE:txn_hash 主键冲突(重复导入)时静默跳过该行
        result = con.execute(
            """
            INSERT OR IGNORE INTO transactions (
                txn_hash, account, record_date, txn_date, action, raw_symbol,
                description, asset_type, underlying, expiry, strike, option_type,
                quantity, price, fees, amount, source_hash, seq
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING txn_hash
            """,
            [
                txn.txn_hash, txn.account, txn.record_date, txn.txn_date, txn.action,
                txn.raw_symbol, txn.description, txn.asset_type, txn.underlying,
                txn.expiry, txn.strike, txn.option_type, txn.quantity, txn.price,
                txn.fees, txn.amount, txn.source_hash, txn.seq,
            ],
        ).fetchone()
        if result is not None:
            inserted += 1
            by_action[txn.action] = by_action.get(txn.action, 0) + 1

    if inserted:
        con.execute(
            """
            INSERT INTO import_files (file_hash, file_name, account, row_count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (file_hash) DO UPDATE SET
                imported_at = now(), row_count = excluded.row_count
            """,
            [digest, path.name, account, inserted],
        )

    return {
        "file": path.name,
        "account": account,
        "total_rows": len(txns),
        "inserted": inserted,
        "skipped": len(txns) - inserted,
        "by_action": by_action,
    }
