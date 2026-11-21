"""Small, durable account ledger backed by SQLite."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from typing import Any
from uuid import uuid4


_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_MAX_AMOUNT_CENTS = 9_000_000_000_000_000


class LedgerError(Exception):
    """Base class for expected ledger errors."""


class AccountNotFound(LedgerError):
    """Raised when an account does not exist."""


class InsufficientFunds(LedgerError):
    """Raised when a debit would make the account balance negative."""


class IdempotencyConflict(LedgerError):
    """Raised when an idempotency key is reused with a different request."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_as_of(value: str | None) -> str:
    if value is None:
        return _utc_now()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("at must be an ISO 8601 timestamp with a timezone") from exc
    if parsed.tzinfo is None:
        raise ValueError("at must include a timezone")
    parsed_utc = parsed.astimezone(timezone.utc)
    if parsed_utc > datetime.now(timezone.utc):
        raise ValueError("at cannot be in the future")
    return parsed_utc.isoformat(timespec="microseconds").replace("+00:00", "Z")


class Ledger:
    """Append-only financial movements with serialized SQLite writes."""

    def __init__(self, database_path: str | Path = "ledger.db") -> None:
        self.database_path = str(database_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            with connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS accounts (
                        id TEXT PRIMARY KEY,
                        customer_id TEXT NOT NULL,
                        currency TEXT NOT NULL CHECK (length(currency) = 3),
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS transactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        account_id TEXT NOT NULL REFERENCES accounts(id),
                        amount_cents INTEGER NOT NULL CHECK (amount_cents != 0),
                        idempotency_key TEXT NOT NULL,
                        description TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE (account_id, idempotency_key)
                    );
                    CREATE INDEX IF NOT EXISTS transactions_account_time
                        ON transactions(account_id, created_at);
                    """
                )

    def create_account(self, customer_id: str, currency: str = "BRL") -> dict[str, Any]:
        if not isinstance(customer_id, str) or not customer_id.strip():
            raise ValueError("customer_id must be a non-empty string")
        if len(customer_id) > 128:
            raise ValueError("customer_id must be at most 128 characters")
        if not isinstance(currency, str) or not _CURRENCY_PATTERN.fullmatch(currency):
            raise ValueError("currency must be a three-letter uppercase code")

        account = {
            "id": uuid4().hex,
            "customer_id": customer_id,
            "currency": currency,
            "created_at": _utc_now(),
        }
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO accounts (id, customer_id, currency, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        account["id"],
                        account["customer_id"],
                        account["currency"],
                        account["created_at"],
                    ),
                )
        return account

    def post_transaction(
        self,
        account_id: str,
        direction: str,
        amount_cents: int,
        idempotency_key: str,
        description: str = "",
    ) -> dict[str, Any]:
        if direction not in ("credit", "debit"):
            raise ValueError("direction must be 'credit' or 'debit'")
        if (
            isinstance(amount_cents, bool)
            or not isinstance(amount_cents, int)
            or amount_cents <= 0
            or amount_cents > _MAX_AMOUNT_CENTS
        ):
            raise ValueError("amount_cents must be a positive integer in range")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be a non-empty string")
        if len(idempotency_key) > 128:
            raise ValueError("idempotency_key must be at most 128 characters")
        if not isinstance(description, str) or len(description) > 280:
            raise ValueError("description must be a string of at most 280 characters")

        signed_amount = amount_cents if direction == "credit" else -amount_cents
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            account = connection.execute(
                "SELECT id, currency FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if account is None:
                raise AccountNotFound("account not found")

            existing = connection.execute(
                "SELECT id, amount_cents, description, created_at "
                "FROM transactions WHERE account_id = ? AND idempotency_key = ?",
                (account_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if (
                    existing["amount_cents"] != signed_amount
                    or existing["description"] != description
                ):
                    raise IdempotencyConflict(
                        "idempotency key was already used for a different request"
                    )
                result = self._transaction_result(
                    existing, account_id, account["currency"], replayed=True
                )
                connection.commit()
                return result

            if signed_amount < 0:
                current = connection.execute(
                    "SELECT COALESCE(SUM(amount_cents), 0) AS balance "
                    "FROM transactions WHERE account_id = ?",
                    (account_id,),
                ).fetchone()["balance"]
                if current + signed_amount < 0:
                    raise InsufficientFunds("debit would make the balance negative")

            created_at = _utc_now()
            cursor = connection.execute(
                "INSERT INTO transactions "
                "(account_id, amount_cents, idempotency_key, description, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (account_id, signed_amount, idempotency_key, description, created_at),
            )
            result = {
                "id": cursor.lastrowid,
                "account_id": account_id,
                "amount_cents": signed_amount,
                "currency": account["currency"],
                "description": description,
                "created_at": created_at,
                "replayed": False,
            }
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _transaction_result(
        row: sqlite3.Row, account_id: str, currency: str, replayed: bool
    ) -> dict[str, Any]:
        return {
            "id": row["id"],
            "account_id": account_id,
            "amount_cents": row["amount_cents"],
            "currency": currency,
            "description": row["description"],
            "created_at": row["created_at"],
            "replayed": replayed,
        }

    def get_balance(self, account_id: str, at: str | None = None) -> dict[str, Any]:
        as_of = _parse_as_of(at)
        with closing(self._connect()) as connection:
            account = connection.execute(
                "SELECT id, customer_id, currency FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
            if account is None:
                raise AccountNotFound("account not found")
            balance = connection.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) AS balance "
                "FROM transactions WHERE account_id = ? AND created_at <= ?",
                (account_id, as_of),
            ).fetchone()["balance"]
        return {
            "account_id": account_id,
            "customer_id": account["customer_id"],
            "balance_cents": balance,
            "currency": account["currency"],
            "as_of": as_of,
            "consulted_at": _utc_now(),
        }
