"""Gmail Schwab eConfirm 解析与 CLI 导入测试。"""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from email.message import EmailMessage

import pytest
from typer.testing import CliRunner

from schwab import db as dbmod
from schwab.cli import app
from schwab.email_ingest import GmailMessage, load_gmail_messages, parse_econfirm
from schwab.ingest import IngestError


runner = CliRunner()


def _base64url(raw: bytes) -> str:
    """按 Gmail API 规则生成无填充 Base64URL 测试数据。"""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _trade_block(
    *, symbol: str, description: str, action: str, position_type: str,
    quantity: str, price: str, principal: str, fees: str, amount: str,
) -> str:
    """构造与 Gmail Markdown 正文结构一致的合成成交块。"""
    fee = Decimal(fees)
    fee_section = ""
    if fee:
        commission = fee - Decimal("0.01")
        fee_section = (
            f"\n\n${commission:.2f}\n\nIndustry Fee:\n\n$0.01"
            f"\n\nTotal:\n\n${fee:.2f}"
        )
    return (
        f"\n\nSymbol:\n\n[{symbol}](https://example.invalid/security)"
        f"\n\nSecurity Description:\n\n{description}"
        f"\n\nAction:\n\n{action}"
        f"\n\nSecurity No./CUSIP:\n\n000000000000"
        f"\n\nType:\n\n{position_type}"
        f"\n\nTrade Date:\n\n07/23/26"
        f"\n\nSettle Date:\n\n07/24/26"
        f"\n\nQuantity\n\nPrice\n\nPrincipal\n\nCharge and/or Interest\n\nTotal Amount"
        f"\n\n{quantity}\n\n${price}\n\n${principal}{fee_section}\n\n${amount}"
        f"\n\nAdditional information for this security:"
    )


def _message(body: str, message_id: str = "gmail-message-1") -> GmailMessage:
    """构造可信 Schwab 发件人的完整测试邮件。"""
    return GmailMessage(
        message_id=message_id,
        sender="Schwab Alerts donotreply@mail.schwab.com",
        subject="Schwab eConfirms account ending in 276",
        body="Account ending: 276" + body,
        email_ts="2026-07-24T16:25:59",
    )


@pytest.mark.parametrize(("reported_action", "position_type", "expected"), [
    ("Purchase", "Margin", "Buy to Open"),
    ("Sale", "Margin", "Sell to Close"),
    ("Sale", "Short", "Sell to Open"),
    ("Purchase", "Short", "Buy to Close"),
])
def test_option_action_uses_action_and_type(reported_action, position_type, expected):
    """期权必须组合 Action 与 Type，不能仅凭买卖方向猜测 Open/Close。"""
    is_purchase = reported_action == "Purchase"
    body = _trade_block(
        symbol="INTC 07/31/2026 120.00 C",
        description="INTEL CORP 07/31/2026 $120 Call",
        action=reported_action,
        position_type=position_type,
        quantity="1", price="2.29", principal="229.00", fees="0.66",
        amount="229.66" if is_purchase else "228.34",
    )

    trade = parse_econfirm(_message(body))[0]

    assert trade.action == expected
    assert trade.symbol == "INTC 07/31/2026 120.00 C"
    assert trade.description == "CALL INTEL CORP $120 EXP 07/31/26"
    assert trade.amount == (Decimal("-229.66") if is_purchase else Decimal("228.34"))


def test_unknown_option_type_is_rejected():
    """未知 Type 必须报错，禁止根据现有持仓自动猜测。"""
    body = _trade_block(
        symbol="INTC 07/31/2026 120.00 C",
        description="INTEL CORP 07/31/2026 $120 Call",
        action="Sale", position_type="Unknown",
        quantity="1", price="2.29", principal="229.00", fees="0.66", amount="228.34",
    )

    with pytest.raises(IngestError, match="无法映射 Action/Type"):
        parse_econfirm(_message(body))


def test_load_rejects_search_result_without_body(tmp_path):
    """只有 snippet 的 Gmail 搜索结果不能作为账务输入。"""
    path = tmp_path / "gmail.json"
    path.write_text(json.dumps({"emails": [{
        "id": "message-1",
        "from_": "Schwab Alerts donotreply@mail.schwab.com",
        "subject": "Schwab eConfirms account ending in 276",
        "snippet": "Electronic Trade Confirmation(s)",
    }]}), encoding="utf-8")

    with pytest.raises(IngestError, match="缺少完整 body"):
        load_gmail_messages(path)


def test_load_standard_gmail_api_full_response(tmp_path):
    """CLI 应直接解码 users.messages.get(format=full) 标准响应。"""
    body = "Account ending: 276" + _trade_block(
        symbol="INTC 07/31/2026 120.00 C",
        description="INTEL CORP 07/31/2026 $120 Call",
        action="Sale", position_type="Short",
        quantity="1", price="2.29", principal="229.00", fees="0.66", amount="228.34",
    )
    payload = {
        "id": "gmail-full-1",
        "threadId": "thread-1",
        "internalDate": "1784900000000",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": "Schwab Alerts <donotreply@mail.schwab.com>"},
                {"name": "Subject", "value": "Schwab eConfirms account ending in 276"},
                {"name": "Date", "value": "Fri, 24 Jul 2026 16:25:59 +0000"},
            ],
            "body": {"size": 0},
            "parts": [{
                "mimeType": "text/plain",
                "headers": [{"name": "Content-Type", "value": "text/plain; charset=UTF-8"}],
                "body": {"size": len(body), "data": _base64url(body.encode())},
            }],
        },
    }
    path = tmp_path / "gmail-full.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    messages = load_gmail_messages(path)
    trades = parse_econfirm(messages[0])

    assert messages[0].message_id == "gmail-full-1"
    assert trades[0].action == "Sell to Open"


def test_load_standard_gmail_api_raw_response(tmp_path):
    """CLI 应直接解码 users.messages.get(format=raw) 的完整 MIME。"""
    body = "Account ending: 276" + _trade_block(
        symbol="INTC", description="INTEL CORP", action="Purchase",
        position_type="Margin", quantity="10", price="88.8323",
        principal="888.32", fees="0", amount="888.32",
    )
    mime = EmailMessage()
    mime["From"] = "Schwab Alerts <donotreply@mail.schwab.com>"
    mime["Subject"] = "Schwab eConfirms account ending in 276"
    mime["Date"] = "Fri, 24 Jul 2026 16:25:59 +0000"
    mime.set_content(body)
    path = tmp_path / "gmail-raw.json"
    path.write_text(json.dumps({
        "id": "gmail-raw-1",
        "threadId": "thread-1",
        "raw": _base64url(mime.as_bytes()),
    }), encoding="utf-8")

    messages = load_gmail_messages(path)
    trades = parse_econfirm(messages[0])

    assert messages[0].message_id == "gmail-raw-1"
    assert trades[0].action == "Buy"
    assert trades[0].price == Decimal("88.8323")


def test_load_original_eml_without_conversion(tmp_path):
    """Gmail 下载的原始 .eml 应可原封不动交给 CLI 输入层。"""
    body = "Account ending: 276" + _trade_block(
        symbol="INTC", description="INTEL CORP", action="Purchase",
        position_type="Margin", quantity="10", price="88.8323",
        principal="888.32", fees="0", amount="888.32",
    )
    mime = EmailMessage()
    mime["From"] = "Schwab Alerts <donotreply@mail.schwab.com>"
    mime["Subject"] = "Schwab eConfirms account ending in 276"
    mime["Message-ID"] = "<original-eml-1@example.invalid>"
    mime.set_content(body)
    path = tmp_path / "schwab.eml"
    path.write_bytes(mime.as_bytes())

    messages = load_gmail_messages(path)
    trades = parse_econfirm(messages[0])

    assert messages[0].message_id == "original-eml-1@example.invalid"
    assert trades[0].action == "Buy"


def test_load_standard_gmail_api_full_html_fallback(tmp_path):
    """format=full 没有 text/plain 时应由 CLI 自行提取 HTML 可见文本。"""
    html = (
        "<html><body><div>Account ending: 276</div>"
        "<div>Symbol:</div><div>INTC</div></body></html>"
    )
    path = tmp_path / "gmail-full-html.json"
    path.write_text(json.dumps({
        "id": "gmail-full-html-1",
        "payload": {
            "mimeType": "text/html",
            "headers": [
                {"name": "From", "value": "Schwab Alerts <donotreply@mail.schwab.com>"},
                {"name": "Subject", "value": "Schwab eConfirms account ending in 276"},
                {"name": "Content-Type", "value": "text/html; charset=UTF-8"},
            ],
            "body": {"size": len(html), "data": _base64url(html.encode())},
        },
    }), encoding="utf-8")

    message = load_gmail_messages(path)[0]

    assert "Account ending: 276" in message.body
    assert "Symbol:\n\nINTC" in message.body


def test_gmail_messages_list_is_rejected_with_actionable_error(tmp_path):
    """messages.list 没有正文，错误必须说明需要继续调用 messages.get。"""
    path = tmp_path / "gmail-list.json"
    path.write_text(json.dumps({
        "messages": [{"id": "message-1", "threadId": "thread-1"}],
        "resultSizeEstimate": 1,
    }), encoding="utf-8")

    with pytest.raises(IngestError, match="users.messages.get"):
        load_gmail_messages(path)


def test_stock_price_precision_is_preserved():
    """邮件中的四位股票成交价不得在转换 CSV 时被舍入。"""
    body = _trade_block(
        symbol="INTC", description="INTEL CORP", action="Purchase",
        position_type="Margin", quantity="10", price="88.8323",
        principal="888.32", fees="0", amount="888.32",
    )

    trade = parse_econfirm(_message(body))[0]

    assert trade.action == "Buy"
    assert trade.price == Decimal("88.8323")
    assert trade.csv_row()[5] == "$88.8323"


def test_multiple_zero_fee_stock_fills_are_expanded():
    """同一邮件交易块中的多个股票成交价应拆成独立 FIFO 记录。"""
    body = (
        "\n\nSymbol:\n\nINTC"
        "\n\nSecurity Description:\n\nINTEL CORP"
        "\n\nAction:\n\nPurchase"
        "\n\nSecurity No./CUSIP:\n\n458140100"
        "\n\nType:\n\nMargin"
        "\n\nTrade Date:\n\n07/01/26"
        "\n\nSettle Date:\n\n07/02/26"
        "\n\nQuantity\n\nPrice\n\nPrincipal\n\nCharge and/or Interest\n\nTotal Amount"
        "\n\n10\n\n$134.655\n\n$1,346.55\n\n$1,346.55"
        "\n\n10\n\n$130.30\n\n$1,303.00\n\n$1,303.00"
        "\n\nTotals\n\n20\n\n$2,649.55\n\n$0.00\n\n$2,649.55"
        "\n\nAdditional information for this security:"
    )

    trades = parse_econfirm(_message(body))

    assert [trade.price for trade in trades] == [Decimal("134.655"), Decimal("130.30")]
    assert [trade.amount for trade in trades] == [Decimal("-1346.55"), Decimal("-1303.00")]


def test_import_email_cli_imports_and_is_idempotent(tmp_path):
    """import-email 应复用现有导入校验，并可安全重复处理同一 Gmail JSON。"""
    body = _trade_block(
        symbol="INTC 07/31/2026 120.00 C",
        description="INTEL CORP 07/31/2026 $120 Call",
        action="Sale", position_type="Short",
        quantity="1", price="2.29", principal="229.00", fees="0.66", amount="228.34",
    )
    path = tmp_path / "gmail.json"
    path.write_text(json.dumps({"responses": [{
        "id": "message-1",
        "from_": "Schwab Alerts donotreply@mail.schwab.com",
        "subject": "Schwab eConfirms account ending in 276",
        "body": "Account ending: 276" + body,
        "email_ts": "2026-07-24T16:25:59",
    }]}), encoding="utf-8")
    db_path = tmp_path / "email.duckdb"

    first = runner.invoke(app, [
        "import-email", str(path), "--account", "TST276", "--no-rebuild",
        "--db", str(db_path), "--json",
    ])
    second = runner.invoke(app, [
        "import-email", str(path), "--account", "TST276", "--no-rebuild",
        "--db", str(db_path), "--json",
    ])

    assert first.exit_code == 0, first.stdout
    assert second.exit_code == 0, second.stdout
    assert json.loads(first.stdout)["files"][0]["inserted"] == 1
    assert json.loads(second.stdout)["files"][0]["skipped"] == 1
    con = dbmod.connect(str(db_path))
    row = con.execute(
        "SELECT action, raw_symbol, fees, amount FROM transactions"
    ).fetchone()
    con.close()
    assert row == (
        "Sell to Open", "INTC 07/31/2026 120.00 C",
        Decimal("0.660000"), Decimal("228.3400"),
    )
