# schwab

[English](README.md) | [简体中文](README.zh-CN.md)

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
- 内置账本审计、Gmail 对账、持仓、现金流及完整的高级交易分析。

## 安装

要求 Python 3.11+。推荐从项目目录使用 uv 安装为系统可调用命令：

```bash
uv tool install .
schwab --help
```

更新代码后重新安装：

```bash
uv tool install --force .
```

也可以使用 pipx：

```bash
pipx install .
```

## 快速开始

```bash
schwab import Individual_ACCOUNT_Transactions_EXPORT.csv
schwab rebuild
schwab audit
schwab summary
schwab positions
```

默认数据库位于 `~/.local/share/schwab/schwab.duckdb`。也可以配置：

```bash
export SCHWAB_DB="$PWD/data/schwab.duckdb"
schwab summary

# 等价写法
schwab summary --db data/schwab.duckdb
```

路径优先级为 `--db` > `SCHWAB_DB` > 默认路径。导入和重建命令可以初始化数据库；
查询及分析命令严格只读，路径不存在时返回 `QUERY_DATABASE_ERROR`，不会创建空库。

## 日常更新

```bash
schwab import Individual_ACCOUNT_Transactions_NEW.csv
schwab rebuild
schwab audit
schwab trades --limit 10
schwab positions
```

`import` 只更新原始交易表。持仓、已实现损益和分析依赖 FIFO 产物，因此 CSV 导入
后必须执行 `rebuild`。两个操作均为幂等操作。

一次导入多个文件：

```bash
schwab import \
  Individual_ACCOUNT_Transactions_PART1.csv \
  Individual_ACCOUNT_Transactions_PART2.csv
schwab rebuild
```

系统会严格验证格式、Action、金额勾稽和 FIFO 数量，不会静默跳过错误。

## Gmail eConfirm 导入

`import-email` 可以直接接收：

- Gmail API `users.messages.get(format=full)` JSON；
- Gmail API `users.messages.get(format=raw)` JSON；
- 多个 `messages.get` 响应组成的 JSON 数组；
- 原始 RFC 5322 `.eml`；
- `-`，从标准输入读取上述格式。

```bash
schwab import-email gmail-econfirms.json
schwab import-email schwab-message.eml
gmail-api-command | schwab import-email -
```

首次向空数据库导入邮件时必须指定完整账户标识：

```bash
schwab import-email gmail-econfirms.json --account ACCOUNT000
```

邮件导入默认自动重建 FIFO。eConfirm 只覆盖成交，不能代替官方 CSV 中的股息、
利息、转账、税项、到期和指派记录。建议定期导入官方 CSV 并对账：

```bash
schwab reconcile gmail-econfirms.json
```

## 基础命令

| 命令 | 用途 |
|---|---|
| `schwab import <csv...>` | 验证并幂等导入官方 CSV |
| `schwab import-email <input...>` | 导入 Gmail API JSON 或 `.eml` |
| `schwab reconcile <input...>` | 只读对账 Gmail eConfirm 与数据库 |
| `schwab rebuild` | 重放 FIFO，重建持仓与已实现损益 |
| `schwab audit` | 检查账本字段、引用及 FIFO 交易覆盖 |
| `schwab summary` | 查看账户汇总 |
| `schwab positions [-u SYMBOL]` | 查看当前持仓 |
| `schwab expiring [--days N]` | 查看已过期和即将到期的期权 |
| `schwab trades [OPTIONS]` | 查询交易流水 |
| `schwab realized [OPTIONS]` | 查询已实现损益 |
| `schwab cashflow [OPTIONS]` | 查询转账、股息、利息和税项 |

完整的月度绩效、归因、回撤、持有周期、极端交易、期权风险和压力测试说明见
[高级交易分析](docs/trading-analysis.zh-CN.md)。

## JSON 与自动化

所有命令均支持 `--json`：

```bash
schwab summary --json
schwab positions --json
schwab audit --json
```

已知业务错误同样输出 JSON，并返回非零状态码：

```json
{"ok": false, "error": {"code": "FIFO_REBUILD_ERROR", "message": "..."}}
```

调用方应同时检查退出状态码和 JSON 内容。

## 数据安全

交易 CSV、邮件原文、数据库及备份都包含敏感财务信息，不应提交到 Git、Issue、
日志或公开制品。`.gitignore` 已排除常见 CSV、DuckDB、Gmail、EML、凭据、密钥及
`application-gin.yml`；发布前仍应检查 `git status`。

## 开发

```bash
uv sync --dev
.venv/bin/pytest
```

## License

本项目采用 [MIT License](LICENSE)。
