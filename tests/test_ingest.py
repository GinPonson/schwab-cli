"""ingest 解析与导入的单测:金额/日期/期权符号/校验/幂等。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from schwab.ingest import (
    IngestError,
    import_csv,
    parse_account,
    parse_csv,
    parse_date_field,
    parse_money,
    parse_option_symbol,
)
from tests.conftest import write_csv


class TestParseMoney:
    """嘉信金额格式的解析规则。"""

    @pytest.mark.parametrize("raw,expected", [
        ("$1,234.56", Decimal("1234.56")),
        ("-$1865.66", Decimal("-1865.66")),
        ("$0.66", Decimal("0.66")),
        ("", None),
    ])
    def test_parse(self, raw, expected):
        assert parse_money(raw) == expected

    def test_invalid(self):
        with pytest.raises(IngestError):
            parse_money("abc")


class TestParseDateField:
    """Date 列两种形式:普通日期与 as-of 日期。"""

    def test_plain(self):
        assert parse_date_field("07/28/2026") == (date(2026, 7, 28), date(2026, 7, 28))

    def test_as_of(self):
        # 生效日取 as-of 之后的日期,record_date 保留前一个
        assert parse_date_field("05/18/2026 as of 05/15/2026") == (
            date(2026, 5, 18), date(2026, 5, 15))

    def test_invalid(self):
        with pytest.raises(IngestError):
            parse_date_field("2026-07-28")


class TestOptionSymbol:
    """期权合约符号 "GOOG 03/19/2027 385.00 C" 的解析。"""

    def test_call(self):
        assert parse_option_symbol("GOOG 03/19/2027 385.00 C") == (
            "GOOG", date(2027, 3, 19), Decimal("385.00"), "C")

    def test_put_integer_strike(self):
        assert parse_option_symbol("INTC 05/15/2026 55.00 P") == (
            "INTC", date(2026, 5, 15), Decimal("55.00"), "P")

    def test_plain_stock_not_option(self):
        assert parse_option_symbol("INTC") is None


class TestAccount:
    def test_parse(self):
        assert parse_account("Individual_XXX276_Transactions_20260730-084033.csv") == "XXX276"

    def test_unknown_format(self):
        with pytest.raises(IngestError):
            parse_account("random.csv")


class TestImport:
    """整文件导入:分类、勾稽、未知 Action、幂等。"""

    def test_import_and_classify(self, tmp_path, con):
        path = write_csv(tmp_path, [
            '"07/28/2026","Buy","INTC","INTEL CORP","10","$85.23","","-$852.30"',
            '"07/28/2026","Buy to Open","GOOG 03/19/2027 385.00 C","CALL ALPHABET","1","$18.65","$0.66","-$1865.66"',
            '"05/13/2026 as of 05/12/2026","MoneyLink Transfer","","Tfr WISE","","","","$5900.00"',
        ])
        stats = import_csv(con, path)
        assert stats["inserted"] == 3 and stats["skipped"] == 0
        rows = con.execute(
            "SELECT asset_type, underlying, expiry, strike, option_type, txn_date "
            "FROM transactions ORDER BY seq"
        ).fetchall()
        assert rows[0] == ("stock", "INTC", None, None, None, date(2026, 7, 28))
        assert rows[1] == ("option", "GOOG", date(2027, 3, 19), Decimal("385.00"), "C", date(2026, 7, 28))
        # as-of:生效日为 05/12
        assert rows[2] == ("cash", None, None, None, None, date(2026, 5, 12))

    def test_unknown_action_aborts(self, tmp_path, con):
        path = write_csv(tmp_path, [
            '"07/28/2026","Wheel Deal","INTC","INTEL CORP","10","$85.23","","-$852.30"',
        ])
        with pytest.raises(IngestError, match="未知 Action"):
            import_csv(con, path)
        # 整个文件不入库,避免半导入状态
        assert con.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0

    def test_amount_mismatch_aborts(self, tmp_path, con):
        path = write_csv(tmp_path, [
            '"07/28/2026","Buy","INTC","INTEL CORP","10","$85.23","","-$999.99"',
        ])
        with pytest.raises(IngestError, match="金额勾稽不符"):
            import_csv(con, path)

    def test_option_amount_uses_multiplier(self, tmp_path, con):
        # 期权金额 = 张数 * 权利金 * 100 - 费用;用错乘数会勾稽失败
        path = write_csv(tmp_path, [
            '"07/15/2026","Sell to Close","WMT 03/19/2027 125.00 C","CALL WALMART","2","$7.63","$1.37","$1524.63"',
        ])
        assert import_csv(con, path)["inserted"] == 1

    def test_reimport_is_idempotent(self, tmp_path, con):
        path = write_csv(tmp_path, [
            '"07/28/2026","Buy","INTC","INTEL CORP","10","$85.23","","-$852.30"',
        ])
        assert import_csv(con, path)["inserted"] == 1
        again = import_csv(con, path)
        assert again["inserted"] == 0 and again["skipped"] == 1

    def test_identical_rows_both_imported(self, tmp_path, con):
        # 同日两行完全相同的 $5 转账是真实存在的,必须靠 seq 区分而非误判重复
        path = write_csv(tmp_path, [
            '"10/28/2025 as of 10/27/2025","MoneyLink Transfer","","Tfr WISE","","","","$5.00"',
            '"10/28/2025 as of 10/27/2025","MoneyLink Transfer","","Tfr WISE","","","","$5.00"',
        ])
        assert import_csv(con, path)["inserted"] == 2

    def test_overlapping_export_deduplicates_when_seq_changes(self, tmp_path, con):
        """后续导出增加新行时，历史交易即使行号变化也不得重复入账。"""
        old_path = write_csv(
            tmp_path,
            ['"07/28/2026","Buy","INTC","INTEL CORP","10","$85.23","","-$852.30"'],
            name="Individual_TST001_Transactions_20260728-000000.csv",
        )
        new_path = write_csv(
            tmp_path,
            [
                '"07/29/2026","Qualified Dividend","INTC","INTEL CORP","","","","$5.00"',
                '"07/28/2026","Buy","INTC","INTEL CORP","10","$85.23","","-$852.30"',
            ],
            name="Individual_TST001_Transactions_20260729-000000.csv",
        )

        assert import_csv(con, old_path)["inserted"] == 1
        stats = import_csv(con, new_path)

        assert stats["inserted"] == 1
        assert stats["skipped"] == 1
        assert con.execute("SELECT count(*) FROM transactions").fetchone()[0] == 2

    def test_overlapping_export_preserves_identical_transaction_count(self, tmp_path, con):
        """完全相同的真实交易按出现次数去重，而不是合并成一笔。"""
        row = '"10/28/2025 as of 10/27/2025","MoneyLink Transfer","","Tfr WISE","","","","$5.00"'
        old_path = write_csv(
            tmp_path, [row],
            name="Individual_TST001_Transactions_20251028-000000.csv",
        )
        new_path = write_csv(
            tmp_path, [row, row],
            name="Individual_TST001_Transactions_20251029-000000.csv",
        )

        import_csv(con, old_path)
        stats = import_csv(con, new_path)

        assert stats["inserted"] == 1
        assert stats["skipped"] == 1
        assert con.execute("SELECT count(*) FROM transactions").fetchone()[0] == 2

    def test_every_valid_file_is_registered_with_total_rows(self, tmp_path, con):
        """重复文件也应留存导入审计记录，row_count 始终表示文件总行数。"""
        path = write_csv(tmp_path, [
            '"07/28/2026","Buy","INTC","INTEL CORP","10","$85.23","","-$852.30"',
        ])
        import_csv(con, path)
        import_csv(con, path)

        assert con.execute(
            "SELECT row_count FROM import_files WHERE file_name = ?", [path.name]
        ).fetchone()[0] == 1

    def test_database_failure_rolls_back_whole_file(self, tmp_path, con):
        """任一行写库失败时，前面已插入的行及文件登记都必须回滚。"""
        path = write_csv(tmp_path, [
            '"07/29/2026","Buy","WMT","WALMART INC","1","$100.00","","-$100.00"',
            '"07/28/2026","Buy","INTC","INTEL CORP","10","$85.23","","-$852.30"',
        ])

        class FailingConnection:
            """在第二条交易写入时注入数据库异常的最小连接代理。"""

            def __init__(self, connection):
                self.connection = connection
                self.transaction_inserts = 0

            def execute(self, sql, params=None):
                if sql.lstrip().startswith("INSERT OR IGNORE INTO transactions"):
                    self.transaction_inserts += 1
                    if self.transaction_inserts == 2:
                        raise RuntimeError("injected database failure")
                return self.connection.execute(sql, params or [])

        with pytest.raises(RuntimeError, match="injected database failure"):
            import_csv(FailingConnection(con), path)

        assert con.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM import_files").fetchone()[0] == 0
