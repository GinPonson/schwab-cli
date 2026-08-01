# Advanced trading analysis

[English](trading-analysis.md) | [简体中文](trading-analysis.zh-CN.md)

This guide documents the advanced analysis commands, calculation conventions,
and limitations of `schwab`. Before running analysis, import the latest records
and rebuild the FIFO products:

```bash
schwab rebuild
schwab audit
```

Every command supports `--db PATH` and `--json`. Date options use ISO
`YYYY-MM-DD` format.

## Quick analysis workflow

```bash
# 1. Monthly results and sources of profit
schwab monthly
schwab attribution

# 2. Drawdown and trading behavior
schwab drawdown
schwab holding-period
schwab extremes --limit 10
schwab streaks

# 3. Current allocation and option risk
schwab allocation
schwab risk
schwab stress
```

## Monthly performance

```bash
schwab monthly
schwab monthly --from 2030-01-01 --to 2030-12-31
```

`monthly` groups FIFO realized records by closing month:

- `closed_lots`: number of FIFO lots consumed;
- `close_transactions`: number of distinct closing executions;
- `wins` / `losses`: profitable and losing lots;
- `win_rate_pct`: `wins / (wins + losses)`;
- `gross_profit` / `gross_loss`: positive and negative P&L totals;
- `profit_factor`: `gross_profit / abs(gross_loss)`;
- `net_pnl`: realized P&L after allocated opening and closing fees;
- `fees`: fees allocated to the realized records.

When there are no losses, `profit_factor` is `null`. The CLI does not invent an
arbitrary infinity or fallback value.

## Realized P&L attribution

```bash
schwab attribution
schwab attribution --group-by asset-type
schwab attribution --group-by direction
schwab attribution --group-by close-action
schwab attribution --from 2030-01-01 --to 2030-12-31
```

Supported dimensions:

| Dimension | Meaning |
|---|---|
| `underlying` | Stock or option underlying; the default |
| `asset-type` | Stocks versus options |
| `direction` | Direction of the closed position: `long` or `short` |
| `close-action` | `Sell`, `Sell to Close`, `Buy to Close`, `Expired`, or `Assigned` |

`absolute_contribution_pct` divides each group's absolute net P&L by the sum of
absolute net P&L across all groups. Positive and negative groups therefore do not
cancel each other. It measures contribution to outcome variation, not a simple
percentage decomposition of net profit.

## Realized drawdown

```bash
schwab drawdown
schwab drawdown --from 2030-01-01 --to 2030-12-31
```

`drawdown` first aggregates realized P&L by closing date, then calculates:

- `daily_pnl`: realized P&L for the date;
- `cumulative_pnl`: cumulative realized P&L within the selected range;
- `running_peak`: highest cumulative P&L reached so far;
- `drawdown`: `running_peak - cumulative_pnl`;
- `max_drawdown`: largest realized drawdown in the range;
- `peak_date`, `trough_date`, and `recovery_date`: dates of the maximum drawdown.

This is a realized P&L curve and excludes changes in open positions. It is not an
account-equity drawdown. When `--from` is used, the selected range starts from a
new cumulative P&L baseline of `$0`.

## Current allocation

```bash
schwab allocation
schwab allocation --group-by asset-type
```

The default grouping is `underlying`; `asset-type` is also supported. Stock cost
is `remaining quantity × opening price`. Option cost also applies the standard
contract multiplier of 100.

The output `basis` is `historical_cost_not_market_value`. The command does not
connect to a market-data provider, so percentages represent historical cost, not
current market value.

## Option expiration and concentration risk

```bash
schwab risk
schwab risk --as-of 2030-01-01
```

`risk` groups current open options by time remaining from the analysis date:

- `expired`;
- `0-30d`;
- `31-60d`;
- `61-90d`;
- `90d+`.

Each bucket reports long and short position counts, contract counts, premium paid
for long options, and premium received for short options. The two premium types
are never added together: premium paid can describe historical long-option
investment, while premium received does not represent a short option's maximum risk.

Concentration uses long premium paid only. Its summary fields are
`top_three_long_underlyings` and `top_three_long_concentration_pct`. Short options
report contracts and historical premium received without inferring risk exposure
from that income.

The command reports expired positions that remain open in the ledger. It does not
guess whether they expired, were assigned, or should be deleted. Import an official
Transactions CSV to record the actual outcome.

## Holding period

```bash
schwab holding-period
```

The command calculates `close_date - open_date` for each FIFO lot and groups it
into `0-7d`, `8-30d`, `31-90d`, and `90d+`. It compares win rate, average holding
days, average lot P&L, and net P&L.

A single closing execution may consume lots opened on different dates. Therefore,
`close_transactions` can overlap across buckets and must not be summed across
buckets as a global execution count.

## Best and worst trades

```bash
schwab extremes
schwab extremes --limit 20
```

`extremes` combines multiple FIFO realized rows sharing the same `close_txn`
before returning the best and worst N closing trades. This prevents an execution
that spans several lots from being counted multiple times.

Results include the contract, direction, quantity, closing action, fees, and the
earliest and latest opening dates of the consumed lots.

## Winning and losing streaks

```bash
schwab streaks
```

`streaks` uses the same closing-transaction aggregation as `extremes`. It orders
trades by closing date and stable transaction key, then reports the longest winning
and losing runs. A zero-P&L trade ends the current run and is counted separately in
`neutral_transactions`.

When same-day executions lack a stable broker timestamp, their sequence is
deterministic but cannot be interpreted as exact intraday order. This does not
affect a same-day streak when every execution has the same result direction.

## Long-option stress test

```bash
schwab stress
```

`stress` analyzes current long options by underlying and reports:

- contract count;
- premium paid;
- maximum loss if the options expire worthless;
- percentage of total long-option premium;
- nearest and farthest expiration dates.

`all_long_options_loss_if_zero` is the premium loss if every current long option
expires worthless. `top_three_loss_if_zero` applies the same scenario to the three
underlyings with the highest premium paid.

This is a hypothetical upper-bound premium scenario. It contains no probability,
correlation, underlying price, implied volatility, or Greeks. It also does not
model the potentially unlimited loss of uncovered short options.

## JSON examples

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

The stable `basis` field describes the data foundation of each report. Automation
must not ignore it or interpret historical-cost and realized-P&L reports as
real-time account-equity reports.

## Shared conventions and limitations

- Every P&L analysis uses the FIFO `realized` table after allocated fees.
- `closed_lots` counts FIFO lots, not orders or closing executions.
- `close_transactions` deduplicates by the stable imported closing transaction hash.
- Unrealized P&L, current market value, total account equity, and buying power
  cannot be derived reliably from transaction history alone.
- Option reports do not include the current underlying price, ITM/ATM/OTM state,
  break-even distance, implied volatility, or Delta, Gamma, Theta, and Vega.
- Drawdown uses realized P&L rather than daily account equity and cannot be compared
  directly with broker or benchmark-index returns.
- Historical-cost concentration reflects opening cost or premium paid, not current
  risk exposure. Short premium received remains separate from long premium paid.
- eConfirm covers executions only. Complete analysis still depends on official CSV
  records for expiration, assignment, cash, and tax activity.

Real-time market value, unrealized P&L, Greeks, correlation stress, and benchmark
comparison require a trusted current and historical market-data source plus explicit
rules for adjustments, valuation timestamps, and missing prices.
