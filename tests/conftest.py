"""pytest 公共 fixture:临时 DuckDB 与嘉信 CSV 构造工具。"""

from __future__ import annotations

from pathlib import Path

import pytest

from schwab import db as dbmod

# 嘉信导出 CSV 的标准表头(与真实导出一致)
CSV_HEADER = '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"'


@pytest.fixture()
def con(tmp_path):
    """指向临时目录的全新数据库连接,每个用例独立。"""
    connection = dbmod.connect(str(tmp_path / "test.duckdb"))
    yield connection
    connection.close()


def write_csv(tmp_path: Path, rows: list[str], name: str = "Individual_TST001_Transactions_20260101-000000.csv") -> Path:
    """把若干行(不含表头)写成标准嘉信 CSV,返回文件路径。"""
    path = tmp_path / name
    path.write_text(CSV_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path
