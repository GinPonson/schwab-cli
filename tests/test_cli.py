"""CLI 机器可读错误协议测试。"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from schwab import db as dbmod
from schwab.cli import app
from tests.conftest import write_csv


runner = CliRunner()


def test_import_json_error_is_structured(tmp_path):
    """--json 模式下导入错误应返回稳定代码，且不得输出 Rich 文本。"""
    path = write_csv(tmp_path, [
        '"07/28/2026","Unknown Action","INTC","INTEL CORP","10","$85.23","","-$852.30"',
    ])

    result = runner.invoke(
        app, ["import", str(path), "--db", str(tmp_path / "cli.duckdb"), "--json"]
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INGEST_VALIDATION_ERROR"


def test_rebuild_json_error_is_structured(tmp_path):
    """FIFO 数据不一致时返回 JSON 错误，而不是 Python traceback。"""
    path = write_csv(tmp_path, [
        '"01/06/2026","Sell","INTC","INTEL CORP","20","$60.00","","$1200.00"',
        '"01/05/2026","Buy","INTC","INTEL CORP","10","$40.00","","-$400.00"',
    ])
    db_path = tmp_path / "cli.duckdb"
    imported = runner.invoke(app, ["import", str(path), "--db", str(db_path), "--json"])
    assert imported.exit_code == 0

    result = runner.invoke(app, ["rebuild", "--db", str(db_path), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "FIFO_REBUILD_ERROR"


def test_expiring_classifies_expired_and_upcoming_positions(tmp_path):
    """expiring 应相对基准日区分已过期与窗口内即将到期仓位。"""
    path = write_csv(tmp_path, [
        '"07/02/2026","Buy to Open","OLD 07/31/2026 100.00 C","CALL OLD $100 EXP 07/31/26","1","$1.00","$0.66","-$100.66"',
        '"07/01/2026","Buy to Open","NEW 08/15/2026 100.00 C","CALL NEW $100 EXP 08/15/26","2","$2.00","$1.31","-$401.31"',
    ])
    db_path = tmp_path / "expiring.duckdb"
    assert runner.invoke(app, ["import", str(path), "--db", str(db_path)]).exit_code == 0
    assert runner.invoke(app, ["rebuild", "--db", str(db_path)]).exit_code == 0

    result = runner.invoke(app, [
        "expiring", "--days", "30", "--as-of", "2026-08-01",
        "--db", str(db_path), "--json",
    ])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["expired"] == 1
    assert payload["upcoming"] == 1
    assert payload["expires_today"] == 0
    assert payload["rows"][0]["days_remaining"] == -1


def test_audit_monthly_and_allocation_use_current_ledger(tmp_path):
    """审计、月度绩效和配置分析应直接读取当前 FIFO 账本。"""
    path = write_csv(tmp_path, [
        '"03/03/2026","Sell","AAA","AAA CORP","5","$12.00","$1.00","$59.00"',
        '"03/01/2026","Buy","AAA","AAA CORP","10","$10.00","$1.00","-$101.00"',
        '"02/02/2026","Sell to Close","BBB 06/19/2026 50.00 C","CALL BBB $50 EXP 06/19/26","1","$3.00","$1.00","$299.00"',
        '"02/01/2026","Buy to Open","BBB 06/19/2026 50.00 C","CALL BBB $50 EXP 06/19/26","1","$2.00","$1.00","-$201.00"',
    ])
    db_path = tmp_path / "analysis.duckdb"
    assert runner.invoke(app, ["import", str(path), "--db", str(db_path)]).exit_code == 0
    assert runner.invoke(app, ["rebuild", "--db", str(db_path)]).exit_code == 0

    audit_result = runner.invoke(app, ["audit", "--db", str(db_path), "--json"])
    assert audit_result.exit_code == 0, audit_result.stdout
    audit_payload = json.loads(audit_result.stdout)
    assert audit_payload["ok"] is True
    assert audit_payload["transaction_count"] == 4

    monthly_result = runner.invoke(app, ["monthly", "--db", str(db_path), "--json"])
    assert monthly_result.exit_code == 0, monthly_result.stdout
    monthly_payload = json.loads(monthly_result.stdout)
    assert monthly_payload["months"] == 2
    # 股票开仓费按平仓数量分摊 0.50，再计入 1.00 平仓费。
    assert monthly_payload["total_pnl"] == "106.5000"
    assert monthly_payload["rows"][0]["month"] == "2026-02"
    assert monthly_payload["rows"][0]["win_rate_pct"] == "100.00"

    allocation_result = runner.invoke(
        app, ["allocation", "--db", str(db_path), "--json"]
    )
    assert allocation_result.exit_code == 0, allocation_result.stdout
    allocation_payload = json.loads(allocation_result.stdout)
    assert allocation_payload["groups"] == 1
    assert allocation_payload["rows"][0]["category"] == "AAA"
    assert allocation_payload["rows"][0]["cost_basis"] == "50.00"
    assert allocation_payload["rows"][0]["allocation_pct"] == "100.00"


def test_monthly_rejects_invalid_date_range(tmp_path):
    """月度报告不得静默接受反向日期范围。"""
    db_path = tmp_path / "empty.duckdb"
    # 显式初始化数据库；只读查询不应承担创建数据库的副作用。
    dbmod.connect(str(db_path)).close()
    result = runner.invoke(app, [
        "monthly", "--from", "2026-02-01", "--to", "2026-01-01",
        "--db", str(db_path), "--json",
    ])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "INVALID_ARGUMENT"


def test_attribution_drawdown_and_risk_use_fifo_data(tmp_path):
    """归因、回撤和期权风险应使用 FIFO 产物并保持稳定 JSON 口径。"""
    path = write_csv(tmp_path, [
        '"04/02/2026","Sell","CCC","CCC CORP","1","$50.00","$1.00","$49.00"',
        '"04/01/2026","Buy","CCC","CCC CORP","1","$100.00","$1.00","-$101.00"',
        '"03/03/2026","Sell","AAA","AAA CORP","1","$120.00","$1.00","$119.00"',
        '"03/01/2026","Buy","AAA","AAA CORP","1","$100.00","$1.00","-$101.00"',
        '"02/02/2026","Sell to Open","EEE 08/15/2026 60.00 C","CALL EEE $60 EXP 08/15/26","1","$1.00","$1.00","$99.00"',
        '"02/01/2026","Buy to Open","DDD 08/15/2026 50.00 C","CALL DDD $50 EXP 08/15/26","2","$2.00","$1.00","-$401.00"',
    ])
    db_path = tmp_path / "risk.duckdb"
    assert runner.invoke(app, ["import", str(path), "--db", str(db_path)]).exit_code == 0
    assert runner.invoke(app, ["rebuild", "--db", str(db_path)]).exit_code == 0

    attribution_result = runner.invoke(
        app, ["attribution", "--db", str(db_path), "--json"]
    )
    assert attribution_result.exit_code == 0, attribution_result.stdout
    attribution_payload = json.loads(attribution_result.stdout)
    assert attribution_payload["total_pnl"] == "-34.0000"
    assert attribution_payload["rows"][0]["category"] == "AAA"
    assert attribution_payload["rows"][1]["category"] == "CCC"

    drawdown_result = runner.invoke(app, ["drawdown", "--db", str(db_path), "--json"])
    assert drawdown_result.exit_code == 0, drawdown_result.stdout
    drawdown_payload = json.loads(drawdown_result.stdout)
    assert drawdown_payload["max_drawdown"] == "52.0000"
    assert drawdown_payload["peak_date"] == "2026-03-03"
    assert drawdown_payload["trough_date"] == "2026-04-02"
    assert drawdown_payload["recovery_date"] is None

    risk_result = runner.invoke(app, [
        "risk", "--as-of", "2026-08-01", "--db", str(db_path), "--json",
    ])
    assert risk_result.exit_code == 0, risk_result.stdout
    risk_payload = json.loads(risk_result.stdout)
    assert risk_payload["option_positions"] == 2
    assert risk_payload["total_long_premium_paid"] == "400.00"
    assert risk_payload["total_short_premium_received"] == "100.00"
    assert risk_payload["rows"][0]["bucket"] == "0-30d"
    assert risk_payload["rows"][0]["long_premium_paid"] == "400.00"
    assert risk_payload["rows"][0]["short_premium_received"] == "100.00"
    assert risk_payload["top_three_long_underlyings"][0]["underlying"] == "DDD"

    holding_result = runner.invoke(
        app, ["holding-period", "--db", str(db_path), "--json"]
    )
    assert holding_result.exit_code == 0, holding_result.stdout
    holding_payload = json.loads(holding_result.stdout)
    assert holding_payload["rows"][0]["bucket"] == "0-7d"
    assert holding_payload["rows"][0]["closed_lots"] == 2

    extremes_result = runner.invoke(
        app, ["extremes", "--limit", "1", "--db", str(db_path), "--json"]
    )
    assert extremes_result.exit_code == 0, extremes_result.stdout
    extremes_payload = json.loads(extremes_result.stdout)
    assert extremes_payload["rows"][0]["side"] == "best"
    assert extremes_payload["rows"][0]["underlying"] == "AAA"
    assert extremes_payload["rows"][1]["side"] == "worst"
    assert extremes_payload["rows"][1]["underlying"] == "CCC"

    streaks_result = runner.invoke(app, ["streaks", "--db", str(db_path), "--json"])
    assert streaks_result.exit_code == 0, streaks_result.stdout
    streaks_payload = json.loads(streaks_result.stdout)
    assert streaks_payload["close_transactions"] == 2
    assert {row["kind"]: row["count"] for row in streaks_payload["rows"]} == {
        "win": 1, "loss": 1,
    }

    stress_result = runner.invoke(app, ["stress", "--db", str(db_path), "--json"])
    assert stress_result.exit_code == 0, stress_result.stdout
    stress_payload = json.loads(stress_result.stdout)
    assert stress_payload["long_option_contracts"] == "2.000000"
    assert stress_payload["all_long_options_loss_if_zero"] == "-400.00"
    assert stress_payload["rows"][0]["underlying"] == "DDD"


def test_audit_detects_transactions_imported_after_rebuild(tmp_path):
    """导入新持仓但遗漏 rebuild 时，审计必须报告陈旧 FIFO 产物。"""
    first = write_csv(tmp_path, [
        '"01/01/2026","Buy","AAA","AAA CORP","1","$10.00","","-$10.00"',
    ], name="Individual_TST001_Transactions_A.csv")
    second = write_csv(tmp_path, [
        '"01/02/2026","Buy","BBB","BBB CORP","1","$20.00","","-$20.00"',
    ], name="Individual_TST001_Transactions_B.csv")
    db_path = tmp_path / "stale.duckdb"
    assert runner.invoke(app, ["import", str(first), "--db", str(db_path)]).exit_code == 0
    assert runner.invoke(app, ["rebuild", "--db", str(db_path)]).exit_code == 0
    assert runner.invoke(app, ["import", str(second), "--db", str(db_path)]).exit_code == 0

    result = runner.invoke(app, ["audit", "--db", str(db_path), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    stale = next(row for row in payload["rows"] if row["check"] == "unreflected_fifo_transactions")
    assert stale["status"] == "fail"
    assert stale["count"] == 1


def test_query_does_not_create_missing_database(tmp_path):
    """只读查询路径写错时应明确失败，且不得创建空数据库。"""
    db_path = tmp_path / "missing.duckdb"

    result = runner.invoke(app, ["summary", "--db", str(db_path), "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "QUERY_DATABASE_ERROR"
    assert not db_path.exists()


def test_basic_queries_reject_invalid_dates_and_limits(tmp_path):
    """基础查询参数错误应使用稳定 JSON 协议，而不是泄漏 DuckDB traceback。"""
    db_path = tmp_path / "query.duckdb"
    dbmod.connect(str(db_path)).close()
    cases = [
        ["realized", "--from", "not-a-date"],
        ["cashflow", "--from", "2026-02-01", "--to", "2026-01-01"],
        ["trades", "--limit", "-1"],
    ]
    for args in cases:
        result = runner.invoke(app, [*args, "--db", str(db_path), "--json"])
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"]["code"] == "INVALID_ARGUMENT"
