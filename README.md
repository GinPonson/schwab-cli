# schwab

嘉信(Schwab)本地交易记录 CLI:把嘉信导出的 Transactions CSV 清洗、幂等导入
DuckDB,按 FIFO 重建持仓与已实现损益,所有命令支持 `--json` 输出,方便 AI 调用。

## 安装

要求 Python 3.11 或更高版本。推荐使用 uv 以隔离环境安装到当前用户，安装完成后
可以在任意目录直接运行 `schwab`：

```bash
uv tool install .
uv tool update-shell
schwab --help
```

`uv tool update-shell` 只需在命令无法找到时执行一次；执行后重新打开终端。如果
已经安装旧版本，在项目目录更新代码后执行：

```bash
uv tool install --force .
```

卸载：

```bash
uv tool uninstall schwab
```

也可以使用 pipx 安装到当前用户的独立环境：

```bash
pipx install .
schwab --help
```

上述方式不会把依赖安装进系统 Python，但会把 `schwab` 命令放入当前用户的
`PATH`，适合日常使用。项目开发环境仍可使用 `uv sync --dev` 创建 `.venv`。

## 快速开始

```bash
schwab import Individual_ACCOUNT_Transactions_*.csv
schwab rebuild
schwab summary
schwab positions
```

数据库路径解析顺序:`--db` 参数 > `SCHWAB_DB` 环境变量 >
`~/.local/share/schwab/schwab.duckdb`。

## 数据库配置

推荐把数据库放在项目的 `data/` 目录，并在当前终端设置环境变量：

```bash
export SCHWAB_DB="$PWD/data/schwab.duckdb"
```

设置后，后续命令会自动使用该数据库。也可以不设置环境变量，在每条命令中
显式指定路径：

```bash
schwab summary --db data/schwab.duckdb
```

环境变量只对当前终端会话生效。打开新终端后，需要重新设置。

## 标准使用流程

### 首次导入

嘉信导出的文件名需要包含账户标识，格式应类似
`Individual_ACCOUNT_Transactions_EXPORT.csv`。首次使用时执行：

```bash
schwab import Individual_ACCOUNT_Transactions_EXPORT.csv
schwab rebuild
schwab summary
schwab positions
```

各步骤的作用：

1. `import` 清洗、校验并写入原始交易表。
2. `rebuild` 全量重放交易，生成 FIFO 批次、当前持仓和已实现损益。
3. `summary` 检查账户汇总。
4. `positions` 检查当前未平仓。

### 增量更新

拿到新的嘉信导出文件后，执行一次导入和一次重建：

```bash
schwab import Individual_ACCOUNT_Transactions_NEW.csv
schwab rebuild
```

新文件可以和历史数据存在日期重叠。系统会跳过已存在的交易，只插入新增记录。
`rebuild` 当前采用全量重放，不是增量计算；它是幂等操作，可以安全重复执行。

只运行 `import` 后，`trades` 和 `cashflow` 可以看到新记录，但 `positions`、
`realized` 以及 `summary` 中的持仓和损益仍可能是上一次重建的结果。因此正常
使用时应始终在导入后执行 `rebuild`。

### 一次导入多个文件

```bash
schwab import \
  Individual_ACCOUNT_Transactions_PART1.csv \
  Individual_ACCOUNT_Transactions_PART2.csv
schwab rebuild
```

命令会先校验所有文件，再开始导入。每个文件独立使用数据库事务，单个文件写入
失败时不会留下半导入数据。

## 命令

| 命令 | 说明 |
|---|---|
| `schwab import <csv...>` | 清洗并幂等导入 CSV(支持重叠日期快照,重复交易自动跳过) |
| `schwab import-email <input...>` | 直接解析 Gmail API JSON 或原始 `.eml`，导入并默认重建 FIFO |
| `schwab rebuild` | 全量重放交易,重建 lots / realized(幂等) |
| `schwab positions [--underlying SYMBOL]` | 当前持仓:股票股数、期权合约、成本、方向 |
| `schwab realized [--symbol S] [--from D] [--to D]` | 已实现损益明细与合计 |
| `schwab trades [--symbol S] [--action A] [--from D] [--to D] [-n 50]` | 交易流水查询 |
| `schwab cashflow [--from D] [--to D]` | 出入金/股息/利息/税 |
| `schwab summary` | 账户总览:净入金、已实现损益、未平仓、费用与股息税项 |

## Gmail eConfirm 导入

`import-email` 接受 Gmail 原始输入，不要求用户转换为项目自定义格式。可直接传入：

- Gmail API `users.messages.get(format=full)` 的标准 JSON；
- Gmail API `users.messages.get(format=raw)` 的标准 JSON；
- 多个 `messages.get` 响应组成的 JSON 数组；
- Gmail 下载的原始 `.eml` / RFC 5322 MIME 邮件；
- `-`，从标准输入读取上述任一种内容。

CLI 会自行读取 MIME Header、解码 Base64URL，并从 `text/plain` 或 `text/html`
正文提取 eConfirm。下面是 `format=full` 的标准结构示意：

```json
{
  "id": "gmail-message-id",
  "threadId": "gmail-thread-id",
  "internalDate": "1893542400000",
  "payload": {
    "mimeType": "multipart/alternative",
    "headers": [
      {"name": "From", "value": "Schwab Alerts <donotreply@mail.schwab.com>"},
      {"name": "Subject", "value": "Schwab eConfirms account ending in 000"}
    ],
    "parts": [
      {
        "mimeType": "text/plain",
        "body": {"size": 1234, "data": "Base64URL正文"}
      }
    ]
  }
}
```

`users.messages.list` 只返回邮件 ID，不能直接导入。调用方应先用 Gmail 查询条件
取得 ID，再逐个调用 `users.messages.get(format=full 或 raw)`，将响应对象或对象
数组原样保存为 JSON 后交给 CLI。CLI 也兼容 Codex Gmail 连接器返回的、包含完整
`body` 的 `responses` JSON，但该兼容格式不是主输入协议。

已有同尾号账户时，CLI 会自动解析完整账户标识：

```bash
schwab import-email gmail-econfirms.json --db data/schwab.duckdb
schwab import-email schwab-message.eml --db data/schwab.duckdb
gmail-api-command | schwab import-email - --db data/schwab.duckdb
```

空数据库首次导入时必须显式指定完整账户标识：

```bash
schwab import-email gmail-econfirms.json \
  --account ACCOUNT000 --db data/schwab.duckdb
```

该命令默认在导入后执行 FIFO `rebuild`；只希望写入交易表时可使用
`--no-rebuild`。期权动作由邮件的 `Action` 与 `Type` 联合转换；未知组合、缺失
字段、金额无法勾稽、非 Schwab 发件人以及无法可靠分摊费用的多价格成交都会使
整个输入失败，不会跳过或猜测。

eConfirm 只覆盖成交，不能替代股息、利息、转账、税项、到期和指派等完整账户
流水。仍应定期用官方 Transactions CSV 补全并核对账本。

## 查询示例

```bash
# 查看账户总览
schwab summary

# 查看全部当前持仓，或只查看一个通用标的
schwab positions
schwab positions --underlying SYMBOL

# 查看最近 100 条交易
schwab trades --limit 100

# 按标的、Action 和日期范围查询交易
schwab trades --symbol SYMBOL --action Buy \
  --from 2030-01-01 --to 2030-12-31

# 查看全部或指定标的的已实现损益
schwab realized
schwab realized --symbol SYMBOL

# 查看指定日期范围的已实现损益
schwab realized --from 2030-01-01 --to 2030-12-31

# 查看出入金、股息、利息和税项
schwab cashflow
schwab cashflow --from 2030-01-01 --to 2030-12-31
```

完整参数可以通过命令帮助查看：

```bash
schwab --help
schwab import --help
schwab import-email --help
schwab trades --help
```

## AI 与脚本调用

所有命令接受 `--json`(如 `schwab realized --json`)。已知业务错误同样输出
机器可读 JSON，并以非零状态码退出：

```json
{"ok": false, "error": {"code": "FIFO_REBUILD_ERROR", "message": "..."}}
```

常用机器调用示例：

```bash
schwab summary --json
schwab positions --json
schwab positions --underlying SYMBOL --json
schwab trades --from 2030-01-01 --limit 100 --json
schwab realized --symbol SYMBOL --json
```

脚本应同时检查进程退出状态码和 JSON 内容：退出状态码 `0` 表示成功，非 `0`
表示导入校验、FIFO 重建或命令参数存在错误。

## 导入后核验

每次增量更新后，建议依次检查：

```bash
# 确认导入结果中的新增数和跳过重复数
schwab import Individual_ACCOUNT_Transactions_NEW.csv

# 确认重放数量、未平批次数、已实现记录数及警告
schwab rebuild

# 查看最新交易、账户汇总和持仓
schwab trades --limit 10
schwab summary
schwab positions
```

重复运行同一个文件时，正常结果应为新增 `0`、其余记录均跳过。若 `rebuild`
产生已过期但仍未平仓的期权警告，应确认导出文件是否包含对应的平仓、到期或
行权记录。

## 数据口径

- **日期**:CSV 中 `"MM/DD/YYYY as of MM/DD/YYYY"` 的生效日取 as-of 之后的日期。
- **期权**:合约符号 `SYMBOL 01/17/2030 100.00 C` 解析为标的/到期日/行权价/CP,乘数 100。
- **跨快照去重**:按规范化业务字段和相同记录的出现次数识别交易，不依赖
  CSV 绝对行号；后续导出覆盖历史日期时不会重复记账。
- **FIFO**:按 `txn_date` 升序、同日 `seq` 降序(CSV 最新在前)重放；重叠
  快照会用最新文件重新锚定顺序。如果同一合约的同日交易来自多个无法对齐的
  文件，`rebuild` 会明确报错，不猜测先后顺序。
  `Expired`/`Assigned` 的期权腿按 $0 平仓;`Assigned` 的股票交割腿以 CSV 中
  独立的 Buy/Sell 行进入股票 FIFO。
- **勾稽**:导入时逐行校验 `|amount| == qty × price × 乘数 ± fees`,
  未知 Action / 金额不符直接报错中止,不带病入库。
- **已实现损益**:`(平仓价 − 开仓价) × 数量 × 乘数 − 按数量比例分摊的双边费用`。
- **原子性**:单文件导入和 FIFO 产物重建均使用 DuckDB 事务，失败时保留
  操作前的完整状态。

## 当前边界

- 嘉信 CSV 没有稳定交易 ID；项目以全部业务字段作为等价判断。如果嘉信在
  后续导出中修改了同一历史记录的描述或金额，该记录会被视为新交易并显式保留。
- 未知 Action 会拒绝导入。扩展企业行动、行权或其他资金类型前，应先加入对应
  的匿名化真实 CSV 回归样本，避免凭名称猜测账务语义。

## 常见错误

### `INGEST_VALIDATION_ERROR`

常见原因包括：

- 文件名不符合嘉信导出格式，无法解析账户标识。
- CSV 表头、日期、金额或期权合约格式不符合预期。
- 出现尚未支持的 Action。
- 成交金额与数量、价格、乘数及费用无法勾稽。

不要直接删除错误行或修改金额绕过校验。应保留原始文件，根据错误信息确认嘉信
导出格式；遇到新的 Action 时，先增加匿名化真实样本和对应账务规则。

### `FIFO_REBUILD_ERROR`

常见原因包括：

- 平仓数量超过系统中已有的未平仓数量。
- 缺少更早的开仓交易。
- 同一标的同一天的交易来自多个无法对齐的增量文件，日内顺序不明确。

优先重新导出覆盖相关日期、并包含当日全部交易的完整 CSV，再重新执行 `import`
和 `rebuild`。系统会拒绝猜测 FIFO 顺序，避免生成错误损益。

### 数据库被占用

DuckDB 同一数据库文件不适合被多个写进程同时操作。如果遇到锁或连接冲突，关闭
其他正在使用该数据库的 CLI、Python 或 DuckDB 进程后再重试。

## 本地备份与恢复

所有 CLI 命令结束后，可以直接复制已关闭的 DuckDB 文件进行备份：

```bash
mkdir -p local-backups
cp data/schwab.duckdb local-backups/schwab-YYYYMMDD.duckdb
```

恢复前应先停止所有使用数据库的进程，并先备份当前文件。确认目标备份无误后再
执行覆盖：

```bash
cp local-backups/schwab-YYYYMMDD.duckdb data/schwab.duckdb
```

数据库和备份都包含完整交易与账户数据，必须按照敏感财务数据管理。

## 隐私与数据安全

- 交易 CSV、DuckDB 数据库及其 WAL 文件仅保存在本地，不应提交到版本库、
  Issue、日志或公开制品中。
- 文档、测试和示例只能使用虚构账户、通用标的占位符及合成交易数据，不记录
  真实账户标识、持仓、交易偏好或其他可关联到个人的信息。
- 项目的 `.gitignore` 已排除交易 CSV、DuckDB、备份目录、Gmail/EML 原文、
  本地凭据、Token、私钥和 `application-gin.yml`；发布前仍应执行 `git status`
  检查待提交文件。

## License

本项目采用 [MIT License](LICENSE)。

## 测试

```bash
uv sync --dev
.venv/bin/pytest
```
