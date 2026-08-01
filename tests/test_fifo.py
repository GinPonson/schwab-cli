"""fifo 重放引擎的单测:股票 FIFO、期权多空、Expired/Assigned、超卖、幂等。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from schwab.fifo import FifoError, rebuild
from schwab.ingest import import_csv
from tests.conftest import write_csv


def _realized(con, sql_extra=""):
    return con.execute(
        f"SELECT symbol_key, direction, qty, open_price, close_price, open_fees, close_fees, pnl "
        f"FROM realized {sql_extra} ORDER BY realized_id"
    ).fetchall()


def _open_lots(con):
    return con.execute(
        "SELECT symbol_key, direction, remaining_qty, open_price FROM lots WHERE remaining_qty > 0"
    ).fetchall()


class TestStockFifo:
    """股票先进先出与费用分摊。"""

    def test_fifo_across_lots(self, tmp_path, con):
        # 分两批买入后一次卖出 15 股:先消耗第一批 10 股,再消耗第二批 5 股
        path = write_csv(tmp_path, [
            '"01/10/2026","Sell","INTC","INTEL CORP","15","$60.00","","$900.00"',
            '"01/06/2026","Buy","INTC","INTEL CORP","10","$50.00","","-$500.00"',
            '"01/05/2026","Buy","INTC","INTEL CORP","10","$40.00","","-$400.00"',
        ])
        import_csv(con, path)
        rebuild(con)
        rows = _realized(con)
        assert len(rows) == 2
        # 第一批:10 股,(60-40)*10 = 200
        assert rows[0][2] == Decimal("10") and rows[0][7] == Decimal("200.0000")
        # 第二批:5 股,(60-50)*5 = 50
        assert rows[1][2] == Decimal("5") and rows[1][7] == Decimal("50.0000")
        # 剩余 5 股 @50
        lots = _open_lots(con)
        assert lots == [("INTC", "long", Decimal("5"), Decimal("50.00"))]

    def test_oversell_raises(self, tmp_path, con):
        path = write_csv(tmp_path, [
            '"01/06/2026","Sell","INTC","INTEL CORP","20","$60.00","","$1200.00"',
            '"01/05/2026","Buy","INTC","INTEL CORP","10","$40.00","","-$400.00"',
        ])
        import_csv(con, path)
        with pytest.raises(FifoError, match="超过未平数量"):
            rebuild(con)

    def test_same_day_replay_order(self, tmp_path, con):
        # CSV 最新在前:同日内物理靠后的行(seq 大)发生得更早。
        # 文件中 Sell 在上、Buy 在下,重放必须先 Buy 后 Sell,否则误判超卖。
        path = write_csv(tmp_path, [
            '"01/05/2026","Sell","INTC","INTEL CORP","10","$60.00","","$600.00"',
            '"01/05/2026","Buy","INTC","INTEL CORP","10","$40.00","","-$400.00"',
        ])
        import_csv(con, path)
        rebuild(con)
        rows = _realized(con)
        assert len(rows) == 1 and rows[0][7] == Decimal("200.0000")


class TestOptionFifo:
    """期权:开平仓、到期作废、被行权(乘数 100)。"""

    def test_long_expired_loses_premium(self, tmp_path, con):
        path = write_csv(tmp_path, [
            '"05/18/2026 as of 05/15/2026","Expired","NVDA 05/15/2026 160.00 P","PUT NVIDIA","1","","",""',
            '"04/01/2026","Buy to Open","NVDA 05/15/2026 160.00 P","PUT NVIDIA","1","$2.00","$0.66","-$200.66"',
        ])
        import_csv(con, path)
        rebuild(con)
        rows = _realized(con)
        assert len(rows) == 1
        # 权利方到期作废:亏全部权利金 + 开仓费,按 $0 平仓,生效日为 as-of 日
        assert rows[0][7] == Decimal("-200.6600")
        assert _open_lots(con) == []

    def test_short_expired_keeps_premium(self, tmp_path, con):
        path = write_csv(tmp_path, [
            '"05/18/2026 as of 05/15/2026","Expired","INTC 05/15/2026 55.00 P","PUT INTEL","1","","",""',
            '"04/22/2026","Sell to Open","INTC 05/15/2026 55.00 P","PUT INTEL","1","$1.18","$0.66","$117.34"',
        ])
        import_csv(con, path)
        rebuild(con)
        rows = _realized(con)
        # 义务方到期落袋:118 权利金 - 0.66 费用
        assert len(rows) == 1 and rows[0][7] == Decimal("117.3400")
        assert rows[0][1] == "short"

    def test_assigned_closes_short_at_zero(self, tmp_path, con):
        # covered call 被行权:short 期权按 $0 平掉(权利金落袋),
        # 股票腿由 CSV 独立的 Buy/Sell 行走股票 FIFO
        path = write_csv(tmp_path, [
            '"04/20/2026 as of 04/17/2026","Sell","INTC","INTEL CORP","100","$50.00","$0.12","$4999.88"',
            '"04/20/2026 as of 04/17/2026","Assigned","INTC 04/17/2026 50.00 C","CALL INTEL","1","","",""',
            '"03/23/2026","Sell to Open","INTC 04/17/2026 50.00 C","CALL INTEL","1","$1.16","$0.66","$115.34"',
            '"01/05/2026","Buy","INTC","INTEL CORP","100","$40.00","","-$4000.00"',
        ])
        import_csv(con, path)
        rebuild(con)
        rows = _realized(con)
        assert len(rows) == 2
        option_row = next(r for r in rows if r[0] == "INTC 04/17/2026 50.00 C")
        stock_row = next(r for r in rows if r[0] == "INTC")
        # 期权腿:+116 - 0.66 = 115.34;股票腿:(50-40)*100 - 0.12 = 999.88
        assert option_row[7] == Decimal("115.3400")
        assert stock_row[7] == Decimal("999.8800")

    def test_close_spans_multiple_lots_with_fee_split(self, tmp_path, con):
        path = write_csv(tmp_path, [
            '"07/15/2026","Sell to Close","GOOG 03/19/2027 440.00 C","CALL ALPHABET","2","$23.35","$0.71","$4669.29"',
            '"07/10/2026","Buy to Open","GOOG 03/19/2027 440.00 C","CALL ALPHABET","1","$20.00","$0.66","-$2000.66"',
            '"07/01/2026","Buy to Open","GOOG 03/19/2027 440.00 C","CALL ALPHABET","1","$19.00","$0.66","-$1900.66"',
        ])
        import_csv(con, path)
        rebuild(con)
        rows = _realized(con)
        assert len(rows) == 2
        # 第一批:(23.35-19)*100 - 0.66 开仓费 - 平仓费份额;两批平仓费合计恰为 0.71
        assert rows[0][5] == Decimal("0.66")  # 开仓费(整批耗尽)
        assert sum(r[6] for r in rows) == Decimal("0.71")  # 平仓费分摊总额
        # 总损益 = 毛利 (435+335) - 开仓费 1.32 - 平仓费 0.71
        assert sum(r[7] for r in rows) == Decimal("767.9700")

    def test_expired_without_position_raises(self, tmp_path, con):
        path = write_csv(tmp_path, [
            '"05/18/2026 as of 05/15/2026","Expired","NVDA 05/15/2026 160.00 P","PUT NVIDIA","1","","",""',
        ])
        import_csv(con, path)
        with pytest.raises(FifoError, match="无法确定持仓方向"):
            rebuild(con)


class TestRebuildIdempotent:
    """rebuild 幂等:多次重放结果一致。"""

    def test_rebuild_twice_same_result(self, tmp_path, con):
        path = write_csv(tmp_path, [
            '"01/10/2026","Sell","INTC","INTEL CORP","10","$60.00","","$600.00"',
            '"01/05/2026","Buy","INTC","INTEL CORP","10","$40.00","","-$400.00"',
        ])
        import_csv(con, path)
        first = rebuild(con)
        second = rebuild(con)
        assert first["realized_records"] == second["realized_records"]
        assert first["total_realized_pnl"] == second["total_realized_pnl"] == "200.0000"
        assert con.execute("SELECT count(*) FROM realized").fetchone()[0] == 1

    def test_database_failure_preserves_previous_rebuild(self, tmp_path, con):
        """重建写入失败时，不得清空上一版有效的 lots/realized 结果。"""
        path = write_csv(tmp_path, [
            '"01/10/2026","Sell","INTC","INTEL CORP","10","$60.00","","$600.00"',
            '"01/05/2026","Buy","INTC","INTEL CORP","10","$40.00","","-$400.00"',
        ])
        import_csv(con, path)
        rebuild(con)

        class FailingConnection:
            """在写入新 realized 记录时注入数据库异常的最小连接代理。"""

            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, params=None):
                if sql.lstrip().startswith("INSERT INTO realized"):
                    raise RuntimeError("injected rebuild failure")
                return self.connection.execute(sql, params or [])

        with pytest.raises(RuntimeError, match="injected rebuild failure"):
            rebuild(FailingConnection(con))

        rows = con.execute("SELECT pnl FROM realized").fetchall()
        assert rows == [(Decimal("200.0000"),)]


class TestReplayOrdering:
    """跨文件同日顺序必须可证明，不能使用不可比较的文件行号猜测。"""

    def test_same_symbol_same_day_from_unaligned_files_raises(self, tmp_path, con):
        buy_path = write_csv(
            tmp_path,
            ['"01/05/2026","Buy","INTC","INTEL CORP","10","$40.00","","-$400.00"'],
            name="Individual_TST001_Transactions_20260105-010000.csv",
        )
        sell_path = write_csv(
            tmp_path,
            ['"01/05/2026","Sell","INTC","INTEL CORP","10","$60.00","","$600.00"'],
            name="Individual_TST001_Transactions_20260105-020000.csv",
        )
        import_csv(con, buy_path)
        import_csv(con, sell_path)

        with pytest.raises(FifoError, match="未对齐的 CSV 快照"):
            rebuild(con)
