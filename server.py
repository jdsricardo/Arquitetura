"""HTTP API for the local ledger demonstration."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, urlparse

from ledger import (
    AccountNotFound,
    IdempotencyConflict,
    InsufficientFunds,
    Ledger,
)


_ACCOUNT_PATH = re.compile(r"^/accounts/([0-9a-f]{32})(?:/(balance|transactions))?$")
_MAX_BODY_BYTES = 16_384
_AMOUNT_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]{1,2})?$")


def _amount_to_cents(value: object) -> int:
    if not isinstance(value, str) or not _AMOUNT_PATTERN.fullmatch(value):
        raise ValueError("amount must be a decimal string, for example '12.34'")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("amount must be a valid decimal string") from exc
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
        raise ValueError("amount must be positive and have at most two decimals")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ValueError("amount must have at most two decimals")
    return int(cents)


class LedgerHandler(BaseHTTPRequestHandler):
    ledger: Ledger

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0 or length > _MAX_BODY_BYTES:
            raise ValueError("request body must be between 1 and 16384 bytes")
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("request body must contain valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            page = Path(__file__).with_name("index.html").read_bytes()
            self._send_html(200, page)
            return
        if parsed.path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        match = _ACCOUNT_PATH.fullmatch(parsed.path)
        if match is None or match.group(2) != "balance":
            self._send_json(404, {"error": "not found"})
            return
        query = parse_qs(parsed.query, strict_parsing=False)
        if len(query.get("at", [])) > 1:
            self._send_json(400, {"error": "at may be specified only once"})
            return
        try:
            result = self.ledger.get_balance(
                match.group(1), query.get("at", [None])[0]
            )
        except AccountNotFound as exc:
            self._send_json(404, {"error": str(exc)})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except sqlite3.Error:
            self.log_error("Ledger database operation failed")
            self._send_json(503, {"error": "storage temporarily unavailable"})
        else:
            self._send_json(200, result)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/accounts":
            self._create_account()
            return
        match = _ACCOUNT_PATH.fullmatch(parsed.path)
        if match is None or match.group(2) != "transactions":
            self._send_json(404, {"error": "not found"})
            return
        self._create_transaction(match.group(1))

    def _create_account(self) -> None:
        try:
            payload = self._read_json()
            result = self.ledger.create_account(
                payload.get("customer_id"), payload.get("currency", "BRL")
            )
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except sqlite3.Error:
            self.log_error("Ledger database operation failed")
            self._send_json(503, {"error": "storage temporarily unavailable"})
        else:
            self._send_json(201, result)

    def _create_transaction(self, account_id: str) -> None:
        try:
            payload = self._read_json()
            amount_cents = _amount_to_cents(payload.get("amount"))
            key = self.headers.get("Idempotency-Key")
            if key is None:
                raise ValueError("Idempotency-Key header is required")
            result = self.ledger.post_transaction(
                account_id=account_id,
                direction=payload.get("direction"),
                amount_cents=amount_cents,
                idempotency_key=key,
                description=payload.get("description", ""),
            )
        except AccountNotFound as exc:
            self._send_json(404, {"error": str(exc)})
        except InsufficientFunds as exc:
            self._send_json(409, {"error": str(exc)})
        except IdempotencyConflict as exc:
            self._send_json(409, {"error": str(exc)})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except sqlite3.Error:
            self.log_error("Ledger database operation failed")
            self._send_json(503, {"error": "storage temporarily unavailable"})
        else:
            self._send_json(200 if result["replayed"] else 201, result)

    def log_message(self, format: str, *args: object) -> None:
        # Avoid logging request content or customer identifiers.
        super().log_message(format, *args)


def main() -> None:
    database_path = os.environ.get("LEDGER_DB", "ledger.db")
    handler = type(
        "ConfiguredLedgerHandler",
        (LedgerHandler,),
        {"ledger": Ledger(database_path)},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 8000), handler)
    print(f"Ledger API listening on http://127.0.0.1:8000 (database: {database_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping ledger API.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
