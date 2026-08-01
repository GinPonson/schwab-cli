# schwab

[English](README.md) | [简体中文](README.zh-CN.md)

A local-first command-line ledger for Charles Schwab transactions. It imports
Transactions CSV exports or raw Gmail eConfirm messages into DuckDB, rebuilds
stock and option positions with FIFO accounting, and exposes stable JSON output
for scripts and AI workflows.

> This is an independent personal ledger project and is not affiliated with
> Charles Schwab & Co., Inc. Its output is for recordkeeping and analysis only;
> it is not investment, tax, or accounting advice.

## Features

- Idempotent Schwab Transactions CSV imports across overlapping exports.
- Direct ingestion of Gmail API `full` / `raw` JSON, JSON arrays, and `.eml`.
- FIFO replay for stocks and options, including closes, expiration, and assignment.
- Local DuckDB storage; transaction data is not uploaded by the application.
- Stable `--json` output and non-zero exit codes for known business errors.
- Ledger auditing, Gmail reconciliation, positions, cash flow, and realized P&L.
- Monthly performance, attribution, drawdown, concentration, and option stress analysis.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or
  [pipx](https://pipx.pypa.io/)

## Installation

Install the command into an isolated user environment from the project directory:

```bash
uv tool install .
schwab --help
```

If the command is not on `PATH`, run `uv tool update-shell` once and restart the
terminal. Reinstall after updating the local source:

```bash
uv tool install --force .
```

Alternatively, use pipx:

```bash
pipx install .
```

Uninstall:

```bash
uv tool uninstall schwab
```

## Quick start

Import an official Schwab CSV export, rebuild FIFO products, and inspect the ledger:

```bash
schwab import Individual_ACCOUNT_Transactions_EXPORT.csv
schwab rebuild
schwab audit
schwab summary
schwab positions
```

The default database is `~/.local/share/schwab/schwab.duckdb`. Override it with
an environment variable or the per-command `--db` option:

```bash
export SCHWAB_DB="$PWD/data/schwab.duckdb"
schwab summary

# Equivalent explicit form
schwab summary --db data/schwab.duckdb
```

Database path precedence is `--db` > `SCHWAB_DB` > the default path. Import and
rebuild commands may initialize a database. Query and analysis commands are
strictly read-only: a missing path returns `QUERY_DATABASE_ERROR` instead of
creating an empty database.

## Routine workflow

### Update from an official CSV

```bash
schwab import Individual_ACCOUNT_Transactions_NEW.csv
schwab rebuild
schwab audit
schwab trades --limit 10
schwab positions
```

`import` updates only the raw transaction table. `positions`, `realized`, and
derived reports depend on FIFO products, so always run `rebuild` after a CSV
import. Both operations are idempotent.

Import multiple files at once:

```bash
schwab import \
  Individual_ACCOUNT_Transactions_PART1.csv \
  Individual_ACCOUNT_Transactions_PART2.csv
schwab rebuild
```

All files are validated before writes begin. Invalid formats, unknown Actions,
amount reconciliation failures, and FIFO quantity inconsistencies fail
explicitly; the CLI does not silently skip them.

### Import Gmail eConfirm messages

`import-email` accepts standard Gmail API responses and RFC 5322 mail directly:

- Gmail API `users.messages.get(format=full)` JSON;
- Gmail API `users.messages.get(format=raw)` JSON;
- a JSON array of `messages.get` responses;
- raw RFC 5322 `.eml`;
- `-` to read any supported representation from standard input.

```bash
schwab import-email gmail-econfirms.json
schwab import-email schwab-message.eml
gmail-api-command | schwab import-email -
```

The first email import into an empty database requires the full account identifier:

```bash
schwab import-email gmail-econfirms.json --account ACCOUNT000
```

Email imports rebuild FIFO by default. Use `--no-rebuild` only when you explicitly
want to update raw transactions without refreshing derived products.

eConfirm covers executions, not every dividend, interest, transfer, tax,
expiration, or assignment record. Periodically import an official Transactions
CSV and reconcile the original messages against the ledger:

```bash
schwab reconcile gmail-econfirms.json
```

`matched` means an exact match, `missing` means no corresponding ledger trade,
and `conflict` means a same-date, same-contract record differs in key fields.

### Non-standard connector JSON

The core CLI intentionally rejects connector-specific objects such as
`{id, from, subject, body}`. If google-workspace returns this compact shape,
convert it to standard RFC 5322 messages with the standalone adapter:

```bash
python scripts/google_workspace_to_eml.py \
  google-workspace-get.json \
  --output-dir normalized-emails

schwab reconcile normalized-emails/*.eml
schwab import-email normalized-emails/*.eml
```

The adapter performs no network access, credential loading, Schwab parsing, or
database operations. It rejects search results that contain only a snippet.

## Core commands

| Command | Purpose |
|---|---|
| `schwab import <csv...>` | Validate and idempotently import official CSV files |
| `schwab import-email <input...>` | Import Gmail API JSON or raw `.eml` |
| `schwab reconcile <input...>` | Read-only Gmail eConfirm reconciliation |
| `schwab rebuild` | Replay FIFO and rebuild positions and realized P&L |
| `schwab audit` | Check fields, references, and FIFO transaction coverage |
| `schwab summary` | Show an account summary |
| `schwab positions [-u SYMBOL]` | Show current positions |
| `schwab expiring [--days N]` | Show expired and upcoming options |
| `schwab trades [OPTIONS]` | Query transaction history |
| `schwab realized [OPTIONS]` | Query realized P&L details |
| `schwab cashflow [OPTIONS]` | Query transfers, dividends, interest, and tax |

Common queries:

```bash
schwab positions --underlying SYMBOL
schwab expiring --days 30
schwab trades --symbol SYMBOL --from 2030-01-01 --to 2030-12-31
schwab realized --symbol SYMBOL
schwab cashflow --from 2030-01-01 --to 2030-12-31
```

For advanced commands, metric definitions, and interpretation guidance, see
[Advanced trading analysis](docs/trading-analysis.md).

## JSON and automation

Every command supports `--json`:

```bash
schwab summary --json
schwab positions --json
schwab audit --json
```

Known business errors are also emitted as JSON with a non-zero exit status:

```json
{"ok": false, "error": {"code": "FIFO_REBUILD_ERROR", "message": "..."}}
```

Automation should validate both the process exit status and the JSON payload.

## Accounting conventions

- An `as of` date is used as the effective transaction date when present.
- The option contract multiplier is 100; the stock multiplier is 1.
- FIFO replay uses ascending effective dates and preserves same-day CSV order.
- Opening and closing fees are allocated according to the quantity consumed.
- `Expired` and `Assigned` option legs close at `$0`; stock delivery legs are
  handled through their separate CSV `Buy` / `Sell` records.
- Schwab CSV files do not provide a stable transaction ID. Idempotency uses
  normalized business fields together with duplicate occurrence counts.

See [Advanced trading analysis](docs/trading-analysis.md#shared-conventions-and-limitations) for
reporting conventions and limitations.

## Troubleshooting

### `INGEST_VALIDATION_ERROR`

Check the filename, CSV header, dates, amounts, option symbols, and Action values.
Do not delete failing rows or alter amounts to bypass validation. Add an anonymized
fixture and an explicit accounting rule before supporting a new Action.

### `FIFO_REBUILD_ERROR`

This normally indicates a missing earlier opening trade, a close quantity larger
than the available position, or overlapping exports whose same-day order cannot
be aligned. Export a complete CSV covering the affected date, import it, and run
`rebuild` again.

### Database locking

DuckDB does not support multiple concurrent writers to the same file. Close other
CLI, Python, or DuckDB processes using the database and retry.

## Data security and backups

Transaction CSV files, email messages, databases, and backups contain sensitive
financial data. Do not publish them in Git, issues, logs, or build artifacts. The
project `.gitignore` excludes common CSV, DuckDB, Gmail, EML, credential, key, and
`application-gin.yml` files; still inspect `git status` before publishing.

After closing all database connections, back up the DuckDB file directly:

```bash
mkdir -p local-backups
cp data/schwab.duckdb local-backups/schwab-YYYYMMDD.duckdb
```

Back up the current database before restoring another copy. Treat every database
and backup as sensitive financial data.

## Development

```bash
uv sync --dev
.venv/bin/pytest
```

Project layout:

```text
src/schwab/        CLI, ingestion, email parsing, database, and FIFO engine
tests/             Regression tests built from synthetic data
scripts/           Standalone adapters for non-standard external formats
docs/              Advanced usage and analysis documentation
```

## License

This project is licensed under the [MIT License](LICENSE).
