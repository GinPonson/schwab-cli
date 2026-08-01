# 高级交易分析

[English](trading-analysis.md) | [简体中文](trading-analysis.zh-CN.md)

本文说明 `schwab` 的高级交易分析命令、计算口径和结果限制。开始分析前，应先确保
最新交易已经导入并完成 FIFO 重建：

```bash
schwab rebuild
schwab audit
```

所有命令都支持 `--db PATH` 和 `--json`。日期选项使用 ISO 格式
`YYYY-MM-DD`。

## 快速分析工作流

```bash
# 1. 月度表现与利润来源
schwab monthly
schwab attribution

# 2. 回撤与交易行为
schwab drawdown
schwab holding-period
schwab extremes --limit 10
schwab streaks

# 3. 当前仓位结构与期权风险
schwab allocation
schwab risk
schwab stress
```

## 月度绩效

```bash
schwab monthly
schwab monthly --from 2030-01-01 --to 2030-12-31
```

`monthly` 按平仓月份汇总 FIFO 已实现记录：

- `closed_lots`：被平掉的 FIFO lot 数量；
- `close_transactions`：去重后的平仓成交数量；
- `wins` / `losses`：盈利与亏损 lot 数量；
- `win_rate_pct`：`wins / (wins + losses)`；
- `gross_profit` / `gross_loss`：正收益与负收益合计；
- `profit_factor`：`gross_profit / abs(gross_loss)`；
- `net_pnl`：扣除开仓和平仓费用后的已实现损益；
- `fees`：分摊至已实现记录的费用。

没有亏损时 `profit_factor` 为 `null`，不会使用任意无穷大或默认值掩盖真实口径。

## 已实现损益归因

```bash
schwab attribution
schwab attribution --group-by asset-type
schwab attribution --group-by direction
schwab attribution --group-by close-action
schwab attribution --from 2030-01-01 --to 2030-12-31
```

支持以下维度：

| 维度 | 含义 |
|---|---|
| `underlying` | 按股票或期权标的归因，默认值 |
| `asset-type` | 股票与期权 |
| `direction` | 被平仓持仓的 `long` / `short` 方向 |
| `close-action` | `Sell`、`Sell to Close`、`Buy to Close`、`Expired`、`Assigned` |

`absolute_contribution_pct` 使用各组净损益绝对值之和作为分母，因此盈利与亏损不会
互相抵消。它衡量结果波动由哪些组贡献，不是总利润的普通百分比分解。

## 已实现回撤

```bash
schwab drawdown
schwab drawdown --from 2030-01-01 --to 2030-12-31
```

`drawdown` 先按平仓日汇总已实现损益，再生成：

- `daily_pnl`：当日已实现损益；
- `cumulative_pnl`：区间内累计已实现损益；
- `running_peak`：截至当日的累计峰值；
- `drawdown`：`running_peak - cumulative_pnl`；
- `max_drawdown`：区间内最大已实现回撤；
- `peak_date` / `trough_date` / `recovery_date`：最大回撤的峰、谷及恢复日期。

这是已实现损益曲线，不包含未平仓价格变化，因此不能替代账户净值回撤。使用
`--from` 时，区间起点会重新以累计损益 `$0` 为基线。

## 当前仓位配置

```bash
schwab allocation
schwab allocation --group-by asset-type
```

默认按 `underlying` 聚合，也可以按 `asset-type` 聚合。股票成本按
`剩余数量 × 开仓价` 计算；期权成本额外乘以标准合约乘数 100。

输出中的 `basis` 固定为 `historical_cost_not_market_value`。该命令不连接行情源，
所以配置占比是历史成本占比，不是当前市值占比。

## 期权到期与集中度风险

```bash
schwab risk
schwab risk --as-of 2030-01-01
```

`risk` 将当前未平期权按相对基准日的剩余时间分为：

- `expired`；
- `0-30d`；
- `31-60d`；
- `61-90d`；
- `90d+`。

每个窗口分别统计长期权和短期权的仓位数、合约数、长期权已付权利金及短期权已收
权利金。两种权利金不会相加：长期权已付金额可以描述历史投入，短期权已收金额
不能代表最大风险。

集中度只使用长期权已付权利金，汇总字段为 `top_three_long_underlyings` 和
`top_three_long_concentration_pct`。短期权仅报告合约与历史收入，不用收入金额
推断风险敞口。

该命令会如实展示账本中仍未关闭的过期仓位，但不会自动推断其已到期、被指派或
应当删除。应使用官方 Transactions CSV 补齐结果。

## 持有周期

```bash
schwab holding-period
```

命令按 `close_date - open_date` 将 FIFO lot 分为 `0-7d`、`8-30d`、
`31-90d` 和 `90d+`，并比较胜率、平均持有天数、平均 lot 损益及净损益。

一次平仓成交可能消耗多个不同日期的开仓 lot，因此各区间的
`close_transactions` 可能重叠，不能跨区间直接相加作为全局成交数。

## 最佳与最差交易

```bash
schwab extremes
schwab extremes --limit 20
```

`extremes` 先按 `close_txn` 把同一平仓成交产生的多个 FIFO realized 行合并，再
分别返回最佳和最差的 N 笔交易，避免把一笔成交因跨 lot 而重复计数。

结果包含合约、方向、数量、平仓动作、费用，以及该平仓所消耗 lot 的最早与最晚
开仓日期。

## 连胜与连亏

```bash
schwab streaks
```

`streaks` 使用与 `extremes` 相同的平仓交易聚合口径，按平仓日期和稳定交易键排序，
统计最长连续盈利与连续亏损。零损益交易会终止当前连续区间，并单独计入
`neutral_transactions`。

同日成交缺少券商稳定时间戳时，连续顺序只能保持确定性，不能解释为精确的盘中
先后顺序；同日全部同方向盈亏时不影响连续区间结论。

## 长期权压力测试

```bash
schwab stress
```

`stress` 只分析当前长期权，按标的汇总：

- 合约数量；
- 已支付权利金；
- 全部归零时的最大损失；
- 占长期权权利金的比例；
- 最近与最远到期日。

汇总中的 `all_long_options_loss_if_zero` 表示所有长期权同时归零的权利金损失，
`top_three_loss_if_zero` 表示权利金成本前三标的同时归零的损失。

这是假设性上限场景，不包含概率、相关性、标的价格、隐含波动率或 Greeks，也不
适用于裸卖期权的潜在无限损失分析。

## JSON 使用示例

```bash
schwab monthly --json
schwab attribution --group-by underlying --json
schwab drawdown --json
schwab allocation --json
schwab risk --json
schwab holding-period --json
schwab extremes --limit 10 --json
schwab streaks --json
schwab stress --json
```

稳定的 `basis` 字段说明每个报告的数据基础。自动化调用方不应忽略该字段，也不应
把历史成本或已实现损益报告解释为实时净值报告。

## 统一口径与限制

- 所有损益分析来自 FIFO `realized` 表，已扣除分摊费用。
- `closed_lots` 是 FIFO lot 数量，不等同于订单数或平仓成交数。
- `close_transactions` 以导入后生成的稳定平仓交易哈希去重。
- 未实现盈亏、实时市值、总资产和购买力无法仅由交易流水可靠确定。
- 期权分析不包含标的当前价格、ITM/ATM/OTM、盈亏平衡距离、隐含波动率或
  Delta、Gamma、Theta、Vega。
- 回撤基于已实现损益，不是账户逐日净值；无法与券商或基准指数收益率直接比较。
- 历史成本集中度反映投入权利金或开仓成本，不代表实时风险敞口；短期权收入与
  长期权成本保持分列。
- eConfirm 只覆盖成交；完整分析仍依赖官方 CSV 中的到期、指派、现金和税项记录。

需要实时市值、未实现盈亏、Greeks、相关性压力或基准比较时，必须接入可信的
当前及历史行情数据，并单独定义复权、估值时点和缺失行情处理规则。
