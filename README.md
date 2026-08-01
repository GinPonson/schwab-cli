# schwab

本地优先的 Charles Schwab 交易记录 CLI。它将 Transactions CSV 或 Gmail
eConfirm 原始邮件导入 DuckDB，按 FIFO 重建股票和期权持仓，并提供稳定的 JSON
输出供脚本与 AI 使用。

> 本项目是个人账本工具，与 Charles Schwab & Co., Inc. 无关。输出仅用于记录与
> 分析，不构成投资、税务或会计建议。

## 特性

- 幂等导入 Schwab Transactions CSV，重叠快照不会重复记账。
- 直接读取 Gmail API `full` / `raw` JSON、JSON 数组或原始 `.eml`。
- 股票与期权 FIFO 重放，严格处理开仓、平仓、到期和指派。
- 本地 DuckDB 存储，不上传交易记录。
- 所有命令支持 `--json`，业务错误使用稳定的非零退出码。
- 内置账本审计、Gmail 对账、持仓、现金流和已实现损益查询。
- 提供月度绩效、归因、回撤、集中度和期权压力等高级分析。

## 环境要求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)（推荐）或
  [pipx](https://pipx.pypa.io/)

## 安装

从项目目录安装为当前用户可直接调用的命令：

```bash
uv tool install .
schwab --help
```

如果终端找不到命令，执行一次 `uv tool update-shell` 后重新打开终端。更新本地
代码后重新安装：

```bash
uv tool install --force .
```

也可以使用 pipx：

```bash
pipx install .
```

卸载：

```bash
uv tool uninstall schwab
```

## 快速开始

导入 Schwab CSV，重建 FIFO，再检查账户和持仓：

```bash
schwab import Individual_ACCOUNT_Transactions_EXPORT.csv
schwab rebuild
schwab audit
schwab summary
schwab positions
```

默认数据库位于 `~/.local/share/schwab/schwab.duckdb`。也可以通过环境变量或每条
命令的 `--db` 指定位置：

```bash
export SCHWAB_DB="$PWD/data/schwab.duckdb"
schwab summary

# 等价的显式写法
schwab summary --db data/schwab.duckdb
```

数据库路径优先级为：`--db` > `SCHWAB_DB` > 默认路径。
导入和重建命令可以初始化数据库；查询和分析命令严格只读，路径不存在时会返回
`QUERY_DATABASE_ERROR`，不会创建一个看似正常的空数据库。

## 日常工作流

### 更新官方 CSV

```bash
schwab import Individual_ACCOUNT_Transactions_NEW.csv
schwab rebuild
schwab audit
schwab trades --limit 10
schwab positions
```

`import` 只更新原始交易表；`positions`、`realized` 和部分汇总依赖 FIFO 产物，
因此 CSV 导入后必须运行 `rebuild`。两个操作都是幂等的，可以安全重复执行。

一次导入多个文件：

```bash
schwab import \
  Individual_ACCOUNT_Transactions_PART1.csv \
  Individual_ACCOUNT_Transactions_PART2.csv
schwab rebuild
```

命令会先验证全部文件，再开始写入。格式错误、未知 Action、金额无法勾稽或 FIFO
数量不一致都会明确失败，不会静默跳过。

### 导入 Gmail eConfirm

`import-email` 直接接受外部获取的原始 Gmail 内容，无需转换为项目自定义格式：

- Gmail API `users.messages.get(format=full)` JSON；
- Gmail API `users.messages.get(format=raw)` JSON；
- 多个 `messages.get` 响应组成的 JSON 数组；
- 原始 RFC 5322 `.eml`；
- `-`，从标准输入读取上述任一格式。

```bash
schwab import-email gmail-econfirms.json
schwab import-email schwab-message.eml
gmail-api-command | schwab import-email -
```

首次向空数据库导入邮件时，必须给出完整账户标识：

```bash
schwab import-email gmail-econfirms.json --account ACCOUNT000
```

邮件导入默认自动执行 FIFO 重建。需要只导入原始交易时使用 `--no-rebuild`。

eConfirm 只覆盖成交，不包含全部股息、利息、转账、税项、到期或指派记录。应定期
导入官方 Transactions CSV，并用相同的原始邮件执行只读对账：

```bash
schwab reconcile gmail-econfirms.json
```

`matched` 表示完全匹配，`missing` 表示数据库无对应成交，`conflict` 表示存在同日
同合约记录但关键字段不一致。

## 基础命令

| 命令 | 用途 |
|---|---|
| `schwab import <csv...>` | 验证并幂等导入官方 CSV |
| `schwab import-email <input...>` | 导入 Gmail API JSON 或 `.eml` |
| `schwab reconcile <input...>` | 只读对账 Gmail eConfirm 与数据库 |
| `schwab rebuild` | 全量重放 FIFO，重建持仓与已实现损益 |
| `schwab audit` | 只读检查账本字段、引用及交易是否已反映到 FIFO 产物 |
| `schwab summary` | 查看账户汇总 |
| `schwab positions [-u SYMBOL]` | 查看当前持仓 |
| `schwab expiring [--days N]` | 查看已过期和即将到期的期权 |
| `schwab trades [OPTIONS]` | 查询交易流水 |
| `schwab realized [OPTIONS]` | 查询已实现损益明细 |
| `schwab cashflow [OPTIONS]` | 查询转账、股息、利息和税项 |

常用查询：

```bash
schwab positions --underlying SYMBOL
schwab expiring --days 30
schwab trades --symbol SYMBOL --from 2030-01-01 --to 2030-12-31
schwab realized --symbol SYMBOL
schwab cashflow --from 2030-01-01 --to 2030-12-31
```

高级分析命令、指标定义和使用建议见
[高级交易分析](docs/trading-analysis.md)。

## JSON 与自动化

所有命令均可增加 `--json`：

```bash
schwab summary --json
schwab positions --json
schwab audit --json
```

已知业务错误也会输出 JSON，并返回非零状态码：

```json
{"ok": false, "error": {"code": "FIFO_REBUILD_ERROR", "message": "..."}}
```

调用方应同时检查退出状态码和 JSON 内容。

## 数据口径

- 日期中的 `as of` 日期优先作为交易生效日。
- 期权乘数固定为 100；股票乘数为 1。
- FIFO 按生效日期升序重放，并保留 CSV 的同日交易顺序。
- 开仓和平仓费用按实际消耗数量分摊至已实现损益。
- `Expired` 和 `Assigned` 的期权腿以 `$0` 平仓；股票交割腿按 CSV 的独立
  `Buy` / `Sell` 记录处理。
- CSV 没有稳定交易 ID，幂等键由规范化业务字段和重复出现次数共同确定。

更完整的分析口径及限制见[高级交易分析](docs/trading-analysis.md#统一口径与限制)。

## 故障排查

### `INGEST_VALIDATION_ERROR`

检查文件名、CSV 表头、日期、金额、合约格式及 Action。不要通过删除错误行或修改
金额绕过验证；遇到新 Action 时，应先增加匿名化样本和明确的账务规则。

### `FIFO_REBUILD_ERROR`

通常表示缺少更早的开仓记录、平仓数量超过持仓，或同一合约同日交易来自无法对齐
的快照。重新导出覆盖相关日期且包含当日全部交易的官方 CSV 后再次导入。

### 数据库被占用

DuckDB 文件不适合由多个写进程同时操作。关闭其他正在访问同一数据库的 CLI、
Python 或 DuckDB 进程后重试。

## 数据安全与备份

交易 CSV、邮件原文、数据库和备份都包含敏感财务信息，不应提交到 Git、Issue、
日志或公开制品。`.gitignore` 已排除常见 CSV、DuckDB、Gmail、EML、凭据、密钥及
`application-gin.yml`，发布前仍应检查 `git status`。

关闭所有数据库连接后，可直接复制 DuckDB 文件备份：

```bash
mkdir -p local-backups
cp data/schwab.duckdb local-backups/schwab-YYYYMMDD.duckdb
```

恢复前先备份当前数据库，再确认目标备份无误后覆盖。数据库及备份应按敏感财务
数据管理。

## 开发

```bash
uv sync --dev
.venv/bin/pytest
```

项目结构：

```text
src/schwab/        CLI、导入、邮件解析、数据库和 FIFO 引擎
tests/             合成数据回归测试
docs/              进阶使用与分析文档
```

## License

本项目采用 [MIT License](LICENSE)。
