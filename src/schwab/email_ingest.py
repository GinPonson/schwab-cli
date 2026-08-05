"""解析标准 Gmail API 或 RFC 5322 中的 Schwab eConfirm 并转换为交易 CSV。

核心输入协议仅包括 Gmail ``users.messages.get(format=full/raw)`` 标准响应和
RFC 5322/MIME 邮件。特定连接器的紧凑 JSON 必须先在 CLI 外转换为标准邮件。
解析器只接受已知 Schwab eConfirm 字段组合，任何缺失或歧义都会整体报错。
"""

from __future__ import annotations

import csv
import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

from .ingest import IngestError, OPTION_MULTIPLIER, parse_option_symbol


SCHWAB_SENDER_RE = re.compile(r"(?:^|[<\s])[^<>\s]+@(?:[a-z0-9-]+\.)*schwab\.com(?:>|\s|$)", re.I)
ACCOUNT_SUFFIX_RE = re.compile(
    r"Account\s+ending(?:\s+in\s+|\s*:\s*)([A-Za-z0-9]+)", re.I
)
SYMBOL_SPLIT_RE = re.compile(r"(?:^|\n)\s*Symbol:\s*\n", re.I)
MONEY_RE = re.compile(r"^-?\$[\d,]+(?:\.\d+)?$")

# eConfirm 的 Action 表示成交方向，Type 表示成交后的持仓类型。
# 股票没有 Open/Close 语义；期权必须由两者组合得到精确的 Schwab CSV Action。
OPTION_ACTIONS = {
    ("Purchase", "Margin"): "Buy to Open",
    ("Sale", "Margin"): "Sell to Close",
    ("Sale", "Short"): "Sell to Open",
    ("Purchase", "Short"): "Buy to Close",
}
STOCK_ACTIONS = {"Purchase": "Buy", "Sale": "Sell"}


@dataclass(frozen=True)
class GmailMessage:
    """一封带标准邮件头和一个或多个正文候选的邮件。"""

    message_id: str
    sender: str
    subject: str
    body: str
    email_ts: str | None
    body_candidates: tuple[str, ...] = ()

    def candidate_bodies(self) -> tuple[str, ...]:
        """按稳定顺序返回去重后的正文候选，主正文始终排在第一位。"""
        unique: list[str] = []
        for candidate in (self.body, *self.body_candidates):
            if candidate.strip() and candidate not in unique:
                unique.append(candidate)
        return tuple(unique)


@dataclass(frozen=True)
class EmailTrade:
    """从一封 eConfirm 中提取的一笔可转换成交。"""

    trade_date: date
    action: str
    symbol: str
    description: str
    quantity: Decimal
    price: Decimal
    fees: Decimal
    amount: Decimal

    def csv_row(self) -> list[str]:
        """返回与 Schwab Transactions 导出一致的八列文本。"""
        return [
            self.trade_date.strftime("%m/%d/%Y"), self.action, self.symbol,
            self.description, _plain_decimal(self.quantity), _usd(self.price),
            _usd(self.fees) if self.fees else "", _signed_usd(self.amount),
        ]


class _HTMLTextExtractor(HTMLParser):
    """把邮件 HTML 转为保留字段边界的纯文本。"""

    _BLOCK_TAGS = frozenset({
        "address", "article", "br", "div", "footer", "h1", "h2", "h3", "h4",
        "header", "li", "p", "section", "table", "td", "th", "tr", "ul",
    })

    def __init__(self) -> None:
        """初始化解析器，并启用标准 HTML 实体解码。"""
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """在块级元素边界插入换行，并忽略脚本与样式正文。"""
        if tag in {"script", "style"}:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        """结束块级元素时追加换行，恢复脚本与样式忽略状态。"""
        if tag in {"script", "style"}:
            self.ignored_depth = max(0, self.ignored_depth - 1)
        elif not self.ignored_depth and tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        """保留可见文本内容。"""
        if not self.ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        """返回规范化换行与空白后的纯文本。"""
        raw = "".join(self.parts).replace("\r", "")
        lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in raw.split("\n")]
        return "\n\n".join(line for line in lines if line)


def _plain_decimal(value: Decimal) -> str:
    """输出无科学计数法且不保留无意义尾零的十进制文本。"""
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _usd(value: Decimal) -> str:
    """输出 Schwab CSV 使用的美元格式，并保留邮件中的有效精度。"""
    return f"${_plain_decimal(value)}"


def _signed_usd(value: Decimal) -> str:
    """输出符号位位于美元符号之前的金额，如 ``-$12.34``。"""
    prefix = "-" if value < 0 else ""
    return f"{prefix}${_plain_decimal(abs(value))}"


def _decimal(raw: str, *, where: str) -> Decimal:
    """严格解析邮件中的美元或普通十进制值。"""
    text = raw.strip().replace("$", "").replace(",", "")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise IngestError(f"{where}: 无法解析数值 {raw!r}") from exc


def _date(raw: str, *, where: str) -> date:
    """严格解析 eConfirm 的 ``MM/DD/YY`` 交易日期。"""
    try:
        month, day, year = (int(part) for part in raw.strip().split("/"))
        if year < 100:
            year += 2000
        return date(year, month, day)
    except (TypeError, ValueError) as exc:
        raise IngestError(f"{where}: 无法解析交易日期 {raw!r}") from exc


def _field(block: str, label: str, *, where: str) -> str:
    """从交易块读取一个单行字段，缺失时立即失败。"""
    match = re.search(rf"(?:^|\n)\s*{re.escape(label)}:\s*\n+\s*([^\n]+)", block, re.I)
    if not match:
        raise IngestError(f"{where}: 缺少字段 {label!r}")
    return match.group(1).strip()


def _unwrap_symbol(raw: str) -> str:
    """移除 Gmail Markdown 正文中的链接包装，仅保留合约代码。"""
    match = re.match(r"\[([^\]]+)]\([^\n]+\)", raw.strip())
    return (match.group(1) if match else raw.strip()).strip()


def _decode_base64url(raw: str, *, where: str) -> bytes:
    """解码 Gmail API 使用的无填充 Base64URL 数据。"""
    try:
        padding = "=" * (-len(raw) % 4)
        return base64.urlsafe_b64decode(raw + padding)
    except (ValueError, TypeError) as exc:
        raise IngestError(f"{where}: Gmail Base64URL 数据无效") from exc


def _html_to_text(html: str) -> str:
    """把 HTML 邮件正文转换为 eConfirm 解析器可消费的结构化文本。"""
    parser = _HTMLTextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


def _headers(payload: dict, *, where: str) -> dict[str, str]:
    """把 Gmail API payload.headers 转换为不区分大小写的字典。"""
    records = payload.get("headers")
    if not isinstance(records, list):
        raise IngestError(f"{where}: Gmail payload 缺少标准 headers 数组")
    result: dict[str, str] = {}
    for item in records:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            result[item["name"].lower()] = str(item.get("value") or "")
    return result


def _collect_text_parts(part: dict, *, where: str) -> list[tuple[str, str]]:
    """递归收集 Gmail ``format=full`` 中所有内联文本 MIME 部分。"""
    mime_type = str(part.get("mimeType") or "").lower()
    body = part.get("body")
    if mime_type in {"text/plain", "text/html"} and isinstance(body, dict) and body.get("data"):
        raw = _decode_base64url(str(body["data"]), where=where)
        content_type = next((
            str(item.get("value") or "") for item in part.get("headers", [])
            if isinstance(item, dict) and str(item.get("name") or "").lower() == "content-type"
        ), "")
        charset_match = re.search(r"charset=[\"']?([^;\s\"']+)", content_type, re.I)
        charset = charset_match.group(1) if charset_match else "utf-8"
        try:
            return [(mime_type, raw.decode(charset))]
        except (LookupError, UnicodeDecodeError) as exc:
            raise IngestError(f"{where}: 无法按 charset={charset!r} 解码邮件正文") from exc
    results: list[tuple[str, str]] = []
    for child in part.get("parts") or []:
        if not isinstance(child, dict):
            raise IngestError(f"{where}: Gmail MIME part 必须是对象")
        results.extend(_collect_text_parts(child, where=where))
    return results


def _normalize_text_candidates(parts: list[tuple[str, str]]) -> tuple[str, ...]:
    """把 MIME 文本部分转换为稳定、去重的可解析正文候选。

    ``text/plain`` 保持原文，``text/html`` 仅做通用可见文本提取。候选之间不
    拼接，避免两个 alternative 表示产生重复交易块。
    """
    candidates: list[str] = []
    # 先尝试 plain，再尝试 HTML；最终仍要求所有成功候选生成完全一致的交易。
    for expected_type in ("text/plain", "text/html"):
        for mime_type, content in parts:
            if mime_type != expected_type:
                continue
            candidate = _html_to_text(content) if mime_type == "text/html" else content
            if candidate.strip() and candidate not in candidates:
                candidates.append(candidate)
    return tuple(candidates)


def _mime_text_candidates(parsed, *, where: str) -> tuple[str, ...]:
    """从 RFC 5322 邮件中提取所有内联 plain/HTML 正文候选。"""
    parts: list[tuple[str, str]] = []
    try:
        for part in parsed.walk():
            mime_type = part.get_content_type().lower()
            if mime_type not in {"text/plain", "text/html"}:
                continue
            disposition = part.get_content_disposition()
            if disposition == "attachment":
                continue
            content = part.get_content()
            if not isinstance(content, str):
                raise IngestError(f"{where}: MIME 文本正文解码结果不是字符串")
            parts.append((mime_type, content))
    except IngestError:
        raise
    except Exception as exc:
        raise IngestError(f"{where}: 无法解码 MIME 文本正文: {exc}") from exc
    candidates = _normalize_text_candidates(parts)
    if not candidates:
        raise IngestError(f"{where}: MIME 中没有内联 text/plain 或 text/html 正文")
    return candidates


def _from_gmail_api(record: dict, *, where: str) -> GmailMessage:
    """解析标准 Gmail API ``users.messages.get`` 的 full 或 raw 响应。"""
    message_id = str(record.get("id") or "").strip()
    if not message_id:
        raise IngestError(f"{where}: 缺少 Gmail message id")

    if isinstance(record.get("payload"), dict):
        payload = record["payload"]
        headers = _headers(payload, where=where)
        decoded_parts = _collect_text_parts(payload, where=where)
        if not decoded_parts:
            raise IngestError(
                f"{where}: Gmail format=full 响应不含内联 text/plain 或 text/html 正文；"
                "附件式正文需要先通过 Gmail API 取得 attachment data"
            )
        candidates = _normalize_text_candidates(decoded_parts)
        if not candidates:
            raise IngestError(f"{where}: Gmail format=full 没有可解析的文本正文")
        sender = headers.get("from", "")
        subject = headers.get("subject", "")
        email_ts = headers.get("date") or str(record.get("internalDate") or "") or None
    elif isinstance(record.get("raw"), str):
        raw_message = _decode_base64url(record["raw"], where=where)
        try:
            parsed = BytesParser(policy=policy.default).parsebytes(raw_message)
            candidates = _mime_text_candidates(parsed, where=where)
        except IngestError:
            raise
        except Exception as exc:
            raise IngestError(f"{where}: 无法解析 Gmail format=raw MIME: {exc}") from exc
        sender = str(parsed.get("From") or "")
        subject = str(parsed.get("Subject") or "")
        email_ts = str(parsed.get("Date") or "") or None
    else:
        raise IngestError(
            f"{where}: 不是 Gmail users.messages.get 的 format=full 或 format=raw 响应"
        )

    return GmailMessage(
        message_id=message_id, sender=sender, subject=subject, body=candidates[0],
        email_ts=email_ts, body_candidates=candidates[1:],
    )


def _from_raw_mime(raw_message: bytes, *, where: str) -> GmailMessage:
    """把未经转换的 RFC 5322/MIME 邮件解析为内部邮件对象。"""
    try:
        parsed = BytesParser(policy=policy.default).parsebytes(raw_message)
        candidates = _mime_text_candidates(parsed, where=where)
    except IngestError:
        raise
    except Exception as exc:
        raise IngestError(f"{where}: 无法解析原始 MIME: {exc}") from exc
    # .eml 不保证保留 Gmail 内部 ID；优先使用 Gmail 扩展头和 Message-ID，最后
    # 使用完整 MIME 哈希，确保同一原始邮件重复导入时标识稳定。
    message_id = str(
        parsed.get("X-GM-MSGID") or parsed.get("X-Gmail-Message-ID")
        or parsed.get("Message-ID") or hashlib.sha256(raw_message).hexdigest()
    ).strip(" <>\t\r\n")
    return GmailMessage(
        message_id=message_id,
        sender=str(parsed.get("From") or ""),
        subject=str(parsed.get("Subject") or ""),
        body=candidates[0],
        email_ts=str(parsed.get("Date") or "") or None,
        body_candidates=candidates[1:],
    )


def _normalize_description(symbol: str, security_description: str) -> str:
    """把邮件描述转换为官方 Transactions CSV 的描述风格。"""
    option = parse_option_symbol(symbol)
    if option is None:
        return security_description.strip()
    _, expiry, strike, option_type = option
    suffix = re.compile(r"\s+\d{2}/\d{2}/\d{4}\s+\$[\d.]+\s+(?:Call|Put)$", re.I)
    company = suffix.sub("", security_description.strip())
    kind = "CALL" if option_type == "C" else "PUT"
    return f"{kind} {company} ${_plain_decimal(strike)} EXP {expiry.strftime('%m/%d/%y')}"


def load_gmail_content(data: bytes, *, source_name: str) -> list[GmailMessage]:
    """自动识别 Gmail API JSON 或原始 RFC 5322/MIME 邮件。

    JSON 仅接受 ``users.messages.get`` 的 ``format=full`` / ``format=raw`` 标准
    响应或这些响应组成的数组；非 JSON 输入按原始 RFC 5322/MIME 解析。
    """
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        message = _from_raw_mime(data, where=source_name)
        if not SCHWAB_SENDER_RE.search(message.sender):
            raise IngestError(
                f"{source_name}: 发件人不是可识别的 Schwab 域名: {message.sender!r}"
            )
        if "Schwab eConfirms" not in message.subject:
            raise IngestError(f"{source_name}: 不是 Schwab eConfirms 邮件: {message.subject!r}")
        return [message]

    if isinstance(payload, dict) and "messages" in payload:
        raise IngestError(
            f"{source_name}: Gmail users.messages.list 只返回 message id；"
            "请对每个 id 调用 users.messages.get(format=full 或 raw) 后再导入"
        )
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = [payload]
    else:
        raise IngestError(f"{source_name}: Gmail JSON 顶层必须是邮件对象或对象数组")

    if not records:
        raise IngestError(f"{source_name}: Gmail JSON 中没有邮件")
    messages: list[GmailMessage] = []
    for index, record in enumerate(records, start=1):
        where = f"{source_name} 第{index}封"
        if not isinstance(record, dict):
            raise IngestError(f"{where}: 邮件必须是 JSON 对象")
        if "payload" not in record and "raw" not in record:
            raise IngestError(
                f"{where}: 不是 Gmail users.messages.get(format=full/raw) 标准响应；"
                "特定连接器 JSON 请先在 CLI 外转换为 RFC 5322 .eml"
            )
        message = _from_gmail_api(record, where=where)
        if not SCHWAB_SENDER_RE.search(message.sender):
            raise IngestError(f"{where}: 发件人不是可识别的 Schwab 域名: {message.sender!r}")
        if "Schwab eConfirms" not in message.subject:
            raise IngestError(f"{where}: 不是 Schwab eConfirms 邮件: {message.subject!r}")
        messages.append(message)
    return messages


def load_gmail_messages(path: Path) -> list[GmailMessage]:
    """从文件直接读取 Gmail API JSON 或原始 ``.eml``。"""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise IngestError(f"{path}: 无法读取 Gmail 输入: {exc}") from exc
    return load_gmail_content(data, source_name=path.name)


def account_suffix(message: GmailMessage) -> str:
    """从标准 Subject 与所有正文候选提取一致的账户尾号。"""
    suffixes = {
        match.group(1)
        for source in (message.subject, *message.candidate_bodies())
        for match in ACCOUNT_SUFFIX_RE.finditer(source)
    }
    if not suffixes:
        raise IngestError(f"邮件 {message.message_id}: 缺少 Account ending")
    if len(suffixes) != 1:
        raise IngestError(
            f"邮件 {message.message_id}: Subject 与正文中的账户尾号不一致: "
            f"{sorted(suffixes)}"
        )
    return suffixes.pop()


def parse_econfirm(message: GmailMessage) -> list[EmailTrade]:
    """严格解析所有正文候选，并拒绝候选之间不一致的交易结果。"""
    successes: list[list[EmailTrade]] = []
    errors: list[str] = []
    for index, body in enumerate(message.candidate_bodies(), start=1):
        try:
            trades = _parse_econfirm_body(message.message_id, body)
        except IngestError as exc:
            errors.append(f"候选{index}: {exc}")
            continue
        if trades not in successes:
            successes.append(trades)
    if not successes:
        details = "；".join(errors)
        raise IngestError(f"邮件 {message.message_id}: 所有正文候选解析失败: {details}")
    if len(successes) != 1:
        raise IngestError(f"邮件 {message.message_id}: plain 与 HTML 解析出的交易不一致")
    return successes[0]


def _parse_econfirm_body(message_id: str, body: str) -> list[EmailTrade]:
    """把单个规范化正文候选严格解析为逐笔成交。"""
    parts = SYMBOL_SPLIT_RE.split(body)
    if len(parts) < 2:
        raise IngestError(f"正文中没有交易块")

    trades: list[EmailTrade] = []
    for block_index, raw_block in enumerate(parts[1:], start=1):
        block = raw_block.split("Additional information for this security:", 1)[0]
        block_lines = [line.strip() for line in block.splitlines() if line.strip()]
        normalized_lines = {line.lower() for line in block_lines}
        transaction_labels = {"action:", "trade date:", "quantity", "total amount"}
        is_information_stub = (
            "Additional information for this security:" in raw_block
            and (
                len(block_lines) == 1
                or (
                    "security description:" in normalized_lines
                    and normalized_lines.isdisjoint(transaction_labels)
                )
            )
        )
        if is_information_stub:
            # HTML 候选会在邮件末尾重复 Symbol、链接和 Security Description，
            # 但不含任何成交字段；只跳过这种明确的信息副本，避免掩盖残缺交易。
            continue
        where = f"邮件 {message_id} 第{block_index}笔"
        first_line = block_lines[0] if block_lines else ""
        symbol = _unwrap_symbol(first_line)
        description = _field(block, "Security Description", where=where)
        reported_action = _field(block, "Action", where=where)
        position_type = _field(block, "Type", where=where)
        trade_date = _date(_field(block, "Trade Date", where=where), where=where)

        option = parse_option_symbol(symbol)
        if option is None:
            action = STOCK_ACTIONS.get(reported_action)
        else:
            action = OPTION_ACTIONS.get((reported_action, position_type))
        if action is None:
            raise IngestError(
                f"{where}: 无法映射 Action/Type: {reported_action!r}/{position_type!r}"
            )

        marker = re.search(r"Total Amount\s*\n", block, re.I)
        if not marker:
            raise IngestError(f"{where}: 缺少成交金额表")
        values = [line.strip() for line in block[marker.end():].splitlines() if line.strip()]
        trades.extend(_parse_value_rows(
            values, where=where, trade_date=trade_date, action=action, symbol=symbol,
            description=_normalize_description(symbol, description),
            multiplier=OPTION_MULTIPLIER if option else 1,
        ))
    return trades


def _parse_value_rows(
    values: list[str], *, where: str, trade_date: date, action: str, symbol: str,
    description: str, multiplier: int,
) -> list[EmailTrade]:
    """解析成交金额区域，并在可确定时展开多价格成交。

    单笔期权可包含 commission 与 industry fee；多价格成交仅在各腿金额与费用
    均可直接解析并与 Totals 勾稽时展开，避免自行猜测费用分配。
    """
    # 金额表后的披露文本不属于成交字段；仅接受已知边界，避免吞掉未知格式。
    disclosure_index = next((
        index for index, value in enumerate(values)
        if value.lower().startswith("for the above:")
    ), None)
    if disclosure_index is not None and "Totals" not in values:
        # 含 Totals 的多价格模板也用该文本分隔成交腿，需留给逐腿解析。
        values = values[:disclosure_index]

    if "Totals" in values:
        totals_index = values.index("Totals")
        fill_values = values[:totals_index]
        totals = values[totals_index + 1:]
        if len(totals) < 4:
            raise IngestError(f"{where}: 无法识别多价格成交结构")
        total_fees = _decimal(totals[-2], where=where)
        if fill_values and fill_values[-1].lower().startswith("for the above:"):
            # 新模板在最后一个成交腿与 Totals 之间也保留分隔标记；它结束前一腿，
            # 并不代表后面还有一个空成交腿。
            fill_values = fill_values[:-1]
        separators = [
            index for index, value in enumerate(fill_values)
            if value.lower().startswith("for the above:")
        ]
        rows: list[EmailTrade] = []
        if separators:
            starts = [0, *(index + 1 for index in separators)]
            ends = [*separators, len(fill_values)]
            for start, end in zip(starts, ends):
                leg = fill_values[start:end]
                if len(leg) < 4:
                    raise IngestError(f"{where}: 多价格成交单腿字段不足: {leg!r}")
                qty, price, principal = leg[:3]
                fees, amount = _parse_labeled_fees(leg[3:], where=where)
                rows.append(_build_trade(
                    trade_date, action, symbol, description, qty, price, principal,
                    fees, amount, multiplier, where,
                ))
        else:
            if len(fill_values) % 4 != 0:
                raise IngestError(f"{where}: 无法识别多价格成交结构")
            for offset in range(0, len(fill_values), 4):
                qty, price, principal, amount = fill_values[offset:offset + 4]
                rows.append(_build_trade(
                    trade_date, action, symbol, description, qty, price, principal,
                    Decimal(0), amount, multiplier, where,
                ))
        parsed_fees = sum((row.fees for row in rows), Decimal(0))
        if parsed_fees != total_fees:
            raise IngestError(
                f"{where}: 多价格成交费用无法可靠分摊: "
                f"逐腿合计 {parsed_fees} 与 Totals {total_fees} 不一致"
            )
        if sum(row.quantity for row in rows) != _decimal(totals[0], where=where):
            raise IngestError(f"{where}: 多价格成交数量与 Totals 不一致")
        if sum(abs(row.amount) for row in rows) != _decimal(totals[-1], where=where):
            raise IngestError(f"{where}: 多价格成交金额与 Totals 不一致")
        return rows

    if len(values) < 4:
        raise IngestError(f"{where}: 无法识别成交金额结构: {values!r}")
    quantity, price, principal = values[:3]
    tail = values[3:]

    if len(tail) == 1 and MONEY_RE.match(tail[0]):
        fees = Decimal(0)
        amount = tail[0]
    elif len(tail) == 2 and tail[0].rstrip(":").lower() == "n/a" \
            and MONEY_RE.match(tail[1]):
        # HTML 表格常把无费用单元格渲染为单个 N/A。
        fees = Decimal(0)
        amount = tail[1]
    elif len(tail) == 4 and tail[0].lower() == "n/a:" \
            and tail[2].lower() == "n/a" and MONEY_RE.match(tail[1]) \
            and MONEY_RE.match(tail[3]):
        # plain 模板会同时保留 N/A 标签、零费用值和 N/A 说明。
        fees = _decimal(tail[1], where=where)
        if fees != 0:
            raise IngestError(f"{where}: N/A 费用必须为 0，实际 {fees}")
        amount = tail[3]
    else:
        fees, amount = _parse_labeled_fees(tail, where=where)
    return [_build_trade(
        trade_date, action, symbol, description, quantity, price, principal,
        fees, amount, multiplier, where,
    )]


def _parse_labeled_fees(values: list[str], *, where: str) -> tuple[Decimal, str]:
    """严格解析带标签的费用区域，并返回总费用与成交净额。

    期权模板通常包含 Commission、Industry Fee 和 Total；部分股票卖出模板
    仅包含 Industry Fee，随后可能带一个重复的说明单元格，再直接给出净额。
    部分股票多价格成交使用 [N/A:, $0.00, N/A, $amount] 表示零费用行。
    """
    # 股票多价格成交的零费用行：plain 模板在每条腿后保留 N/A 标签、零 Industry
    # Fee 值、N/A 说明与成交净额。该模板无 Commission 也无 Industry Fee 标签。
    if (
        len(values) == 4
        and values[0].lower() == "n/a:"
        and values[2].lower() == "n/a"
        and MONEY_RE.match(values[1])
        and MONEY_RE.match(values[3])
    ):
        fees = _decimal(values[1], where=where)
        if fees != 0:
            raise IngestError(f"{where}: N/A 费用必须为 0，实际 {fees}")
        return fees, values[3]

    index = 0
    if values and values[0].lower() == "industry fee:":
        # 股票无佣金模板：不猜测缺失字段，仅接受明确的 Industry Fee 标签。
        commission = Decimal(0)
    elif index < len(values) and values[index].lower() == "commission:":
        index += 1
        if index >= len(values) or not MONEY_RE.match(values[index]):
            raise IngestError(f"{where}: Commission 后缺少金额")
        commission = _decimal(values[index], where=where)
        index += 1
        # Schwab 新模板可能在佣金金额后附加一个独立的 Commission 说明单元格。
        if index < len(values) and values[index].lower() == "commission":
            index += 1
    elif index < len(values) and MONEY_RE.match(values[index]):
        # 旧模板仅保留佣金金额，没有 Commission 标签。
        commission = _decimal(values[index], where=where)
        index += 1
    else:
        raise IngestError(f"{where}: 无法识别 Commission 字段: {values!r}")

    if index >= len(values) or values[index].lower() != "industry fee:":
        raise IngestError(f"{where}: 缺少 Industry Fee 标签")
    index += 1
    if index >= len(values) or not MONEY_RE.match(values[index]):
        raise IngestError(f"{where}: Industry Fee 后缺少金额")
    industry_fee = _decimal(values[index], where=where)
    index += 1

    if commission == 0 and values[0].lower() == "industry fee:":
        # plain 正文可能保留一个重复的 Industry Fee 说明单元格，HTML 则会省略。
        if index < len(values) and values[index].lower() == "industry fee":
            index += 1
        if index >= len(values) or not MONEY_RE.match(values[index]):
            raise IngestError(f"{where}: Industry Fee 后缺少成交净额")
        amount = values[index]
        index += 1
        if index != len(values):
            raise IngestError(f"{where}: 费用表包含未知字段: {values[index:]!r}")
        return industry_fee, amount

    if index >= len(values) or values[index].lower() != "total:":
        raise IngestError(f"{where}: 缺少费用 Total 标签")
    index += 1
    if index >= len(values) or not MONEY_RE.match(values[index]):
        raise IngestError(f"{where}: Total 后缺少费用金额")
    total_fees = _decimal(values[index], where=where)
    index += 1
    if index >= len(values) or not MONEY_RE.match(values[index]):
        raise IngestError(f"{where}: 费用表后缺少成交净额")
    amount = values[index]
    index += 1
    if index != len(values):
        raise IngestError(f"{where}: 费用表包含未知字段: {values[index:]!r}")

    fees = commission + industry_fee
    if fees != total_fees:
        raise IngestError(f"{where}: 分项费用与 Total 不一致")
    return fees, amount


def _build_trade(
    trade_date: date, action: str, symbol: str, description: str, quantity_raw: str,
    price_raw: str, principal_raw: str, fees: Decimal, amount_raw: str,
    multiplier: int, where: str,
) -> EmailTrade:
    """构造单笔交易并独立核对邮件本金、净额和买卖符号。"""
    quantity = _decimal(quantity_raw, where=where)
    price = _decimal(price_raw, where=where)
    principal = _decimal(principal_raw, where=where)
    net_amount = _decimal(amount_raw, where=where)
    expected_principal = quantity * price * multiplier
    if abs(principal - expected_principal) > Decimal("0.011"):
        raise IngestError(
            f"{where}: Principal 勾稽不符，实际 {principal}，期望 {expected_principal}"
        )
    is_buy = action in {"Buy", "Buy to Open", "Buy to Close"}
    expected_net = principal + fees if is_buy else principal - fees
    if abs(net_amount - expected_net) > Decimal("0.011"):
        raise IngestError(
            f"{where}: Total Amount 勾稽不符，实际 {net_amount}，期望 {expected_net}"
        )
    signed_amount = -net_amount if is_buy else net_amount
    return EmailTrade(
        trade_date=trade_date, action=action, symbol=symbol, description=description,
        quantity=quantity, price=price, fees=fees, amount=signed_amount,
    )


def write_transactions_csv(path: Path, trades: list[EmailTrade]) -> str:
    """写出标准 Schwab CSV，并返回内容哈希供调用方审计。"""
    header = ["Date", "Action", "Symbol", "Description", "Quantity", "Price", "Fees & Comm", "Amount"]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(header)
        for trade in trades:
            writer.writerow(trade.csv_row())
    return hashlib.sha256(path.read_bytes()).hexdigest()
