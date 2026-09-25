"""Durable receipts for the connected manual-sale command.

This module persists exactly one thing: the *receipt* of a connected sale
command, that is the ``sale_id`` idempotency key, the hash of the request it
was accepted for, and the response a retry must replay.  It is deliberately
not an ERP state store.  The item master, stock, sales, and journals stay in
the in-memory :class:`~atlas_erp.business.Business` domain, so after a restart
a durable store replays the original receipt instead of applying the sale a
second time, while the audit projection is still built from process-local
state.  A peer's retry therefore survives a restart; the business facts behind
the receipt do not.

Two adapters implement the one :class:`SaleCommandStore` interface:

* :class:`InMemorySaleCommandStore` for unit tests and the loopback smoke.
* :class:`PostgresSaleCommandStore` for cross-process durable idempotency.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    import psycopg


class SaleCommandError(ValueError):
    """Raised when a sale command store operation is inconsistent."""


# The caller must run the command now and then complete or abort it.
RESERVED = "reserved"
# The same command already completed; return the stored response.
REPLAY = "replay"
# The sale_id was already used for a different request.
PAYLOAD_CONFLICT = "payload_conflict"
# Another attempt currently holds the reservation for this sale_id.
IN_PROGRESS = "in_progress"

PENDING = "pending"
COMPLETED = "completed"

# The one table this module owns.  The name is a literal here on purpose: no
# caller-supplied value ever reaches a table name.
CREATE_COMMANDS_SQL = """
CREATE TABLE IF NOT EXISTS connected_sale_commands (
    sale_id text PRIMARY KEY,
    request_hash text NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'completed')),
    response jsonb,
    reserved_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
)
"""


@dataclass(frozen=True)
class SaleCommandReservation:
    """The result of reserving one ``sale_id`` for a command attempt.

    ``response`` is only populated for :data:`REPLAY`, where it is the receipt
    the original attempt returned.
    """

    sale_id: str
    outcome: str
    response: dict[str, object] | None = None


class SaleCommandStore(Protocol):
    """Idempotency receipts for the connected manual-sale command.

    An attempt is always ``reserve`` first and then exactly one of ``complete``
    or ``abort``.  ``abort`` releases the key so the same command can be
    attempted again, which is why a failed sale is not burned.
    """

    def reserve(self, sale_id: str, request_hash: str) -> SaleCommandReservation:
        """Claim ``sale_id`` for ``request_hash`` or explain why it is taken."""

        ...

    def complete(self, sale_id: str, response: Mapping[str, object]) -> None:
        """Store the response a caller must see again for a retry."""

        ...

    def abort(self, sale_id: str) -> None:
        """Release a reservation whose command did not succeed."""

        ...

    def close(self) -> None:
        """Release any resource the store holds."""

        ...


@dataclass(frozen=True)
class _Command:
    """One stored command row, shared by both adapters."""

    request_hash: str
    status: str
    response: dict[str, object] | None = None


class InMemorySaleCommandStore:
    """A process-local command store, used by tests and the loopback smoke.

    It gives the same replay and conflict semantics as the PostgreSQL adapter
    within one process, and nothing more: the receipts are lost on restart.
    """

    def __init__(self) -> None:
        self._commands: dict[str, _Command] = {}
        self._lock = threading.Lock()

    def reserve(self, sale_id: str, request_hash: str) -> SaleCommandReservation:
        with self._lock:
            existing = self._commands.get(sale_id)
            if existing is None:
                self._commands[sale_id] = _Command(request_hash, PENDING)
                return SaleCommandReservation(sale_id, RESERVED)
        return _replay_or_conflict(sale_id, existing, request_hash)

    def complete(self, sale_id: str, response: Mapping[str, object]) -> None:
        with self._lock:
            existing = self._commands.get(sale_id)
            if existing is None or existing.status != PENDING:
                raise SaleCommandError(
                    f"no pending sale command to complete: {sale_id}"
                )
            self._commands[sale_id] = _Command(
                existing.request_hash, COMPLETED, dict(response)
            )

    def abort(self, sale_id: str) -> None:
        with self._lock:
            existing = self._commands.get(sale_id)
            if existing is not None and existing.status == PENDING:
                del self._commands[sale_id]

    def close(self) -> None:
        return None


class PostgresSaleCommandStore:
    """A durable command store backed by one PostgreSQL table.

    :meth:`reserve` is safe across server instances: the row is inserted with
    ``ON CONFLICT DO NOTHING`` and the existing row is then inspected, so two
    processes racing on one ``sale_id`` produce one :data:`RESERVED` outcome.

    Known ceiling: a process that dies between ``reserve`` and ``complete``
    leaves a ``pending`` row that is answered as :data:`IN_PROGRESS` forever.
    Recovering a crashed attempt needs a lease or an expiry on ``reserved_at``,
    which this slice deliberately does not have.
    """

    def __init__(self, dsn: str, *, connect_timeout: int = 5) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("database URL must be a non-empty string")
        self._lock = threading.Lock()
        self._connection = _connect(dsn, connect_timeout=connect_timeout)
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create the command table when it does not exist yet."""

        with self._connection.transaction():
            self._connection.execute(CREATE_COMMANDS_SQL)

    def reserve(self, sale_id: str, request_hash: str) -> SaleCommandReservation:
        with self._lock, self._connection.transaction():
            inserted = self._connection.execute(
                "INSERT INTO connected_sale_commands (sale_id, request_hash, status) "
                "VALUES (%s, %s, 'pending') ON CONFLICT (sale_id) DO NOTHING "
                "RETURNING sale_id",
                (sale_id, request_hash),
            ).fetchone()
            if inserted is not None:
                return SaleCommandReservation(sale_id, RESERVED)
            row = self._connection.execute(
                "SELECT request_hash, status, response "
                "FROM connected_sale_commands WHERE sale_id = %s",
                (sale_id,),
            ).fetchone()
            if row is None:
                # The conflicting row was deleted between the insert and the
                # read.  Nothing is reserved and nothing was applied, so report
                # a conflict rather than let a second attempt run the command.
                return SaleCommandReservation(sale_id, PAYLOAD_CONFLICT)
        return _replay_or_conflict(
            sale_id,
            _Command(
                str(row[0]),
                str(row[1]),
                cast("dict[str, object] | None", row[2]),
            ),
            request_hash,
        )

    def complete(self, sale_id: str, response: Mapping[str, object]) -> None:
        with self._lock, self._connection.transaction():
            updated = self._connection.execute(
                "UPDATE connected_sale_commands "
                "SET status = 'completed', response = %s::jsonb, completed_at = now() "
                "WHERE sale_id = %s AND status = 'pending'",
                (json.dumps(response, sort_keys=True), sale_id),
            )
            if updated.rowcount != 1:
                raise SaleCommandError(
                    f"no pending sale command to complete: {sale_id}"
                )

    def abort(self, sale_id: str) -> None:
        with self._lock, self._connection.transaction():
            self._connection.execute(
                "DELETE FROM connected_sale_commands "
                "WHERE sale_id = %s AND status = 'pending'",
                (sale_id,),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _connect(dsn: str, *, connect_timeout: int) -> psycopg.Connection[Any]:
    """Open one connection, importing the driver only when it is needed.

    The import is local so the package keeps working when the optional
    PostgreSQL driver is not installed.  Rows are read positionally, in the
    column order of the two statements above.
    """

    import psycopg

    return psycopg.connect(dsn, connect_timeout=connect_timeout)


def _replay_or_conflict(
    sale_id: str, existing: _Command, request_hash: str
) -> SaleCommandReservation:
    """Classify an existing command row against the request it was reserved for."""

    if existing.status != COMPLETED:
        return SaleCommandReservation(sale_id, IN_PROGRESS)
    if existing.response is None:
        return SaleCommandReservation(sale_id, PAYLOAD_CONFLICT)
    if existing.request_hash != request_hash:
        return SaleCommandReservation(sale_id, PAYLOAD_CONFLICT)
    return SaleCommandReservation(sale_id, REPLAY, dict(existing.response))


__all__ = [
    "COMPLETED",
    "CREATE_COMMANDS_SQL",
    "IN_PROGRESS",
    "InMemorySaleCommandStore",
    "PENDING",
    "PAYLOAD_CONFLICT",
    "PostgresSaleCommandStore",
    "REPLAY",
    "RESERVED",
    "SaleCommandError",
    "SaleCommandReservation",
    "SaleCommandStore",
]
