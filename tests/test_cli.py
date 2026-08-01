"""CLI 机器可读错误协议测试。"""

from __future__ import annotations

import json

from typer.testing import CliRunner

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
