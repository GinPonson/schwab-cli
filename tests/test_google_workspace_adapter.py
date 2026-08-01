"""Standalone google-workspace compact JSON to RFC 5322 adapter tests."""

from __future__ import annotations

import json
import subprocess
import sys
from email import policy
from email.parser import BytesParser
from pathlib import Path

from schwab.email_ingest import account_suffix, load_gmail_messages, parse_econfirm


SCRIPT = Path(__file__).parents[1] / "scripts" / "google_workspace_to_eml.py"


def _trade_body() -> str:
    """Return a complete synthetic Schwab stock execution body."""
    return (
        "\n\nSymbol:\n\nINTC"
        "\n\nSecurity Description:\n\nINTEL CORP"
        "\n\nAction:\n\nPurchase"
        "\n\nSecurity No./CUSIP:\n\n458140100"
        "\n\nType:\n\nMargin"
        "\n\nTrade Date:\n\n07/31/26"
        "\n\nSettle Date:\n\n08/03/26"
        "\n\nQuantity\n\nPrice\n\nPrincipal\n\nCharge and/or Interest"
        "\n\nTotal Amount\n\n10\n\n$90.7154\n\n$907.15"
        "\n\nN/A:\n\n$0.00\n\nN/A\n\n$907.15"
        "\n\nFor the above:\n\nSynthetic disclosure"
        "\n\nAdditional information for this security:"
    )


def test_adapter_converts_compact_array_to_parseable_eml(tmp_path):
    """Adapter output should be standard MIME that the core CLI parses unchanged."""
    source = tmp_path / "compact.json"
    source.write_text(json.dumps([{
        "id": "connector-message-1",
        "threadId": "thread-1",
        "from": "Schwab Alerts <donotreply@mail.schwab.com>",
        "to": "owner@example.invalid",
        "subject": "Schwab eConfirms account ending in 276",
        "date": "Fri, 31 Jul 2026 09:32:23 -0600",
        "body": _trade_body(),
        "labels": ["INBOX"],
    }]), encoding="utf-8")
    output_dir = tmp_path / "eml"

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(source), "--output-dir", str(output_dir)],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    outputs = list(output_dir.glob("*.eml"))
    assert len(outputs) == 1
    parsed = BytesParser(policy=policy.default).parsebytes(outputs[0].read_bytes())
    assert parsed["X-Gmail-Message-ID"] == "connector-message-1"
    message = load_gmail_messages(outputs[0])[0]
    assert account_suffix(message) == "276"
    assert parse_econfirm(message)[0].price.as_tuple().exponent == -4


def test_adapter_rejects_search_metadata_without_body(tmp_path):
    """A search result with only a snippet cannot be converted into complete mail."""
    source = tmp_path / "search.json"
    source.write_text(json.dumps({
        "id": "connector-message-1",
        "from": "Schwab Alerts <donotreply@mail.schwab.com>",
        "subject": "Schwab eConfirms account ending in 276",
        "snippet": "Electronic Trade Confirmation(s)",
    }), encoding="utf-8")
    output_dir = tmp_path / "eml"

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(source), "--output-dir", str(output_dir)],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode == 1
    assert "field 'body' is missing" in result.stderr
    assert not list(output_dir.glob("*.eml"))


def test_adapter_validates_complete_batch_before_writing(tmp_path):
    """A malformed later record must not leave an earlier message converted."""
    source = tmp_path / "mixed.json"
    source.write_text(json.dumps([
        {
            "id": "connector-message-1",
            "from": "Schwab Alerts <donotreply@mail.schwab.com>",
            "subject": "Schwab eConfirms account ending in 276",
            "body": _trade_body(),
        },
        {
            "id": "connector-message-2",
            "from": "Schwab Alerts <donotreply@mail.schwab.com>",
            "subject": "Schwab eConfirms account ending in 276",
            "snippet": "Body was not fetched",
        },
    ]), encoding="utf-8")
    output_dir = tmp_path / "eml"

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(source), "--output-dir", str(output_dir)],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode == 1
    assert "message 2" in result.stderr
    assert not output_dir.exists()


def test_adapter_is_idempotent_and_rejects_non_string_headers(tmp_path):
    """Repeated conversion should be stable; ambiguous header values must fail."""
    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps({
        "id": "connector-message-1",
        "from": "Schwab Alerts <donotreply@mail.schwab.com>",
        "subject": "Schwab eConfirms account ending in 276",
        "body": _trade_body(),
    }), encoding="utf-8")
    output_dir = tmp_path / "eml"
    command = [sys.executable, str(SCRIPT), str(valid), "--output-dir", str(output_dir)]

    first = subprocess.run(command, capture_output=True, text=True, check=False)
    second = subprocess.run(command, capture_output=True, text=True, check=False)

    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout

    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({
        "id": "connector-message-2",
        "from": ["not", "a", "string"],
        "subject": "Schwab eConfirms account ending in 276",
        "body": _trade_body(),
    }), encoding="utf-8")
    rejected = subprocess.run(
        [sys.executable, str(SCRIPT), str(invalid), "--output-dir", str(output_dir)],
        capture_output=True, text=True, check=False,
    )
    assert rejected.returncode == 1
    assert "field 'from' is missing" in rejected.stderr
