# schwab

嘉信(Schwab)个人交易记录 CLI:把嘉信导出的 Transactions CSV 清洗、幂等导入
DuckDB,按 FIFO 重建持仓与已实现损益,所有命令支持 `--json` 输出,方便 AI 调用。

## 安装

```bash
uv sync
```

## 快速开始

```bash
uv run schwab import Individual_XXX276_Transactions_*.csv
uv run schwab rebuild
uv run schwab summary
uv run schwab positions
```

数据库路径解析顺序:`--db` 参数 > `SCHWAB_DB` 环境变量 >
`~/.local/share/schwab/schwab.duckdb`。

## 命令

| 命令 | 说明 |
|---|---|
| `schwab import <csv...>` | 清洗并幂等导入 CSV(行级去重,重复导入自动跳过) |
| `schwab rebuild` | 全量重放交易,重建 lots / realized(幂等) |
| `schwab positions [--underlying INTC]` | 当前持仓:股票股数、期权合约、成本、方向 |
| `schwab realized [--symbol S] [--from D] [--to D]` | 已实现损益明细与合计 |
| `schwab trades [--symbol S] [--action A] [--from D] [--to D] [-n 50]` | 交易流水查询 |
| `schwab cashflow [--from D] [--to D]` | 出入金/股息/利息/税 |
| `schwab summary` | 账户总览:净入金、已实现损益、未平仓、费用与股息税项 |

所有查询命令接受 `--json`(如 `schwab realized --json`)。

## 数据口径

- **日期**:CSV 中 `"MM/DD/YYYY as of MM/DD/YYYY"` 的生效日取 as-of 之后的日期。
- **期权**:合约符号 `GOOG 03/19/2027 385.00 C` 解析为标的/到期日/行权价/CP,乘数 100。
- **FIFO**:按 `txn_date` 升序、同日 `seq` 降序(CSV 最新在前)重放;
  `Expired`/`Assigned` 的期权腿按 $0 平仓;`Assigned` 的股票交割腿以 CSV 中
  独立的 Buy/Sell 行进入股票 FIFO。
- **勾稽**:导入时逐行校验 `|amount| == qty × price × 乘数 ± fees`,
  未知 Action / 金额不符直接报错中止,不带病入库。
- **已实现损益**:`(平仓价 − 开仓价) × 数量 × 乘数 − 按数量比例分摊的双边费用`。

## 测试

```bash
uv run pytest
```
