#!/usr/bin/env python3
"""Convert google-workspace compact Gmail JSON into RFC 5322 ``.eml`` files.

This adapter deliberately lives outside the ``schwab`` package. It performs no
network access, credential loading, Schwab parsing, or database writes. Search
results containing only snippets are rejected because they are not complete mail.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from email.message import EmailMessage
from pathlib import Path


class ConversionError(Exception):
    """Raised when connector JSON cannot be converted without guessing."""


def _required_text(record: dict, field: str, *, where: str) -> str:
    """Return a required non-empty string field or raise a contextual error."""
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ConversionError(f"{where}: required non-empty string field {field!r} is missing")
    return value.strip()


def _optional_text(record: dict, field: str, *, where: str) -> str | None:
    """Return an optional string field while rejecting ambiguous non-string values."""
    value = record.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConversionError(f"{where}: optional field {field!r} must be a string")
    return value.strip() or None


def _load_records(data: bytes, *, source: str) -> list[dict]:
    """Decode a compact connector object or array without accepting wrappers."""
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConversionError(f"{source}: input is not valid UTF-8 JSON: {exc}") from exc
    records = payload if isinstance(payload, list) else [payload]
    if not records or any(not isinstance(record, dict) for record in records):
        raise ConversionError(f"{source}: top level must be a non-empty object or object array")
    return records


def _build_eml(record: dict, *, where: str) -> tuple[str, bytes]:
    """Build one deterministic RFC 5322 text/plain message from a compact object."""
    message_id = _required_text(record, "id", where=where)
    sender = _required_text(record, "from", where=where)
    subject = _required_text(record, "subject", where=where)
    body = _required_text(record, "body", where=where)
    recipient = _optional_text(record, "to", where=where)
    message_date = _optional_text(record, "date", where=where)

    message = EmailMessage()
    try:
        message["From"] = sender
        if recipient:
            message["To"] = recipient
        message["Subject"] = subject
        if message_date:
            message["Date"] = message_date
        # Preserve the upstream identifier for stable repeated imports without
        # pretending that it is an RFC Message-ID supplied by the sender.
        message["X-Gmail-Message-ID"] = message_id
        digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
        message["Message-ID"] = f"<{digest}@google-workspace-adapter.invalid>"
        message.set_content(body)
    except (TypeError, ValueError) as exc:
        raise ConversionError(f"{where}: invalid RFC 5322 header or body: {exc}") from exc
    return digest[:16], message.as_bytes()


def convert(input_path: Path | None, output_dir: Path) -> list[Path]:
    """Convert compact JSON from a file or stdin and return generated paths.

    Existing identical files are treated idempotently. A filename collision with
    different content fails explicitly and is never overwritten.
    """
    if input_path is None:
        data = sys.stdin.buffer.read()
        source = "stdin"
    else:
        try:
            data = input_path.read_bytes()
        except OSError as exc:
            raise ConversionError(f"{input_path}: cannot read input: {exc}") from exc
        source = str(input_path)
    records = _load_records(data, source=source)
    # Validate and serialize the complete batch before creating or writing files.
    # This prevents a malformed later record from leaving a partial conversion.
    prepared: dict[str, bytes] = {}
    for index, record in enumerate(records, start=1):
        where = f"{source} message {index}"
        digest, content = _build_eml(record, where=where)
        if digest in prepared and prepared[digest] != content:
            raise ConversionError(
                f"{where}: duplicate message id resolves to different content"
            )
        prepared[digest] = content

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConversionError(f"{output_dir}: cannot create output directory: {exc}") from exc

    # Preflight every deterministic target before writing any new file. This
    # avoids a late collision leaving only part of the requested batch written.
    existing_targets: set[Path] = set()
    for digest, content in prepared.items():
        target = output_dir / f"gmail-{digest}.eml"
        if target.exists():
            try:
                existing = target.read_bytes()
            except OSError as exc:
                raise ConversionError(f"{target}: cannot verify existing output: {exc}") from exc
            if existing != content:
                raise ConversionError(f"{target}: existing file has different content")
            existing_targets.add(target)

    outputs: list[Path] = []
    for digest, content in prepared.items():
        target = output_dir / f"gmail-{digest}.eml"
        if target in existing_targets:
            outputs.append(target)
            continue
        try:
            # Exclusive creation prevents accidental overwrite if another process
            # creates the deterministic target after the existence check.
            with target.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as exc:
            raise ConversionError(f"{target}: output appeared concurrently; retry explicitly") from exc
        except OSError as exc:
            raise ConversionError(f"{target}: cannot write output: {exc}") from exc
        outputs.append(target)
    return outputs


def main(argv: list[str] | None = None) -> int:
    """Parse command-line arguments, convert messages, and print generated paths."""
    parser = argparse.ArgumentParser(
        description="Convert google-workspace compact Gmail JSON to RFC 5322 .eml files."
    )
    parser.add_argument("input", help="Compact JSON file, or - for stdin")
    parser.add_argument("--output-dir", required=True, type=Path, help="Directory for .eml files")
    args = parser.parse_args(argv)
    try:
        paths = convert(None if args.input == "-" else Path(args.input), args.output_dir)
    except ConversionError as exc:
        parser.exit(1, f"error: {exc}\n")
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
