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
import time
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

# How long one attempt may hold a ``sale_id`` before a later attempt for the
# same key may reclaim it.  This is a loopback profile that reserves, posts one
# sale, and completes, all inside one HTTP request with no queue in front of it,
# so the window only has to outlast a slow in-flight call: milliseconds on
# loopback, and orders of magnitude more if the peer ever stalls.  Thirty
# seconds is that with room to spare, and short enough that a crashed attempt
# stops holding a customer's key before anyone thinks to go looking for it.
# The length is always a positive number of seconds: see :func:`_lease_seconds`.
DEFAULT_LEASE_SECONDS = 30

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
    the original attempt returned.  ``retry_after_seconds`` is only populated
    for :data:`IN_PROGRESS`, where it is the holder's lease: how long the caller
    should wait before trying again, rather than being left to guess whether
    the key is stuck or merely busy.
    """

    sale_id: str
    outcome: str
    response: dict[str, object] | None = None
    retry_after_seconds: int | None = None


class SaleCommandStore(Protocol):
    """Idempotency receipts for the connected manual-sale command.

    An attempt is always ``reserve`` first and then exactly one of ``complete``
    or ``abort``.  ``abort`` releases the key so the same command can be
    attempted again, which is why a failed sale is not burned.  A reservation
    also carries a lease, so an attempt that dies without either call does not
    hold its key forever; see :data:`DEFAULT_LEASE_SECONDS`.
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
    """One stored command row, shared by both adapters.

    ``reserved_at`` is a monotonic reading rather than a wall clock: the lease
    only ever compares one reading against another, and the database adapter
    gets the same thing from ``now()``.
    """

    request_hash: str
    status: str
    response: dict[str, object] | None = None
    reserved_at: float = 0.0


class InMemorySaleCommandStore:
    """A process-local command store, used by tests and the loopback smoke.

    It gives the same replay and conflict semantics as the PostgreSQL adapter
    within one process, and nothing more: the receipts are lost on restart.
    The lease is real here too, against :func:`time.monotonic`, so the in-memory
    path exercises the reclaim rather than only describing it.
    """

    def __init__(self, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
        self._lease_seconds = _lease_seconds(lease_seconds)
        self._commands: dict[str, _Command] = {}
        self._lock = threading.Lock()

    def reserve(self, sale_id: str, request_hash: str) -> SaleCommandReservation:
        with self._lock:
            existing = self._commands.get(sale_id)
            if existing is None:
                self._commands[sale_id] = _Command(
                    request_hash, PENDING, reserved_at=time.monotonic()
                )
                return SaleCommandReservation(sale_id, RESERVED)
            if self._reclaimable(existing, request_hash):
                self._commands[sale_id] = _Command(
                    request_hash, PENDING, reserved_at=time.monotonic()
                )
                return SaleCommandReservation(sale_id, RESERVED)
        return _replay_or_conflict(
            sale_id, existing, request_hash, self._lease_seconds
        )

    def _reclaimable(self, existing: _Command, request_hash: str) -> bool:
        """Say whether a stale claim may be taken over, as the UPDATE would.

        Same two conditions as the ``UPDATE`` the durable adapter runs: the row
        is still ``pending``, and it is the *same* command that reserved it.  A
        different request hash is left alone deliberately, so a slow attempt
        cannot lose its key to an unrelated submission for the same sale_id.
        """

        return (
            existing.status == PENDING
            and existing.request_hash == request_hash
            and time.monotonic() - existing.reserved_at >= self._lease_seconds
        )

    def complete(self, sale_id: str, response: Mapping[str, object]) -> None:
        with self._lock:
            existing = self._commands.get(sale_id)
            if existing is None or existing.status != PENDING:
                raise SaleCommandError(
                    f"no pending sale command to complete: {sale_id}"
                )
            self._commands[sale_id] = _Command(
                existing.request_hash,
                COMPLETED,
                dict(response),
                existing.reserved_at,
            )

    def abort(self, sale_id: str) -> None:
        with self._lock:
            existing = self._commands.get(sale_id)
            if existing is not None and existing.status == PENDING:
                del self._commands[sale_id]

    def close(self) -> None:
        return None


class PostgresSaleCommandStore:
    """A durable command store backed on one PostgreSQL table.

    :meth:`reserve` is safe across server instances twice over.  The row is
    inserted with ``ON CONFLICT DO NOTHING`` and the existing row is then
    inspected, so two processes racing on one ``sale_id`` produce one
    :data:`RESERVED` outcome.  If the row is a ``pending`` claim whose lease has
    expired, the reclaim is a conditional ``UPDATE`` rather than a read followed
    by a write, and only ``rowcount == 1`` counts as having won it, so two
    processes reclaiming one crashed key still produce one :data:`RESERVED`.

    The lease is what a crashed attempt cannot outlive: a process that dies
    between ``reserve`` and ``complete`` leaves a ``pending`` row, and the next
    attempt for that ``sale_id`` takes it over once
    :data:`DEFAULT_LEASE_SECONDS` has passed.  Reclaiming is safe even if the
    first attempt turns out to be slow rather than dead, because the peer's
    ``sale_id`` is derived from the caller's key, so a second attempt re-posts
    the *same* peer sale and the peer refuses the duplicate rather than
    accepting a second one.  What a reclaimed key cannot become is a second
    sale; the worst it can produce is a ``409 duplicate sale`` naming a sale the
    first attempt did make.

    Known ceiling: a ``pending`` row reserved for a *different* request hash is
    still not reclaimable, because a live attempt for that hash may still own
    it.  It answers :data:`IN_PROGRESS` with the lease as its wait hint, and
    releasing it stays a human decision through ``abort``.
    """

    def __init__(
        self, dsn: str, *, connect_timeout: int = 5, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("database URL must be a non-empty string")
        self._lease_seconds = _lease_seconds(lease_seconds)
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
            # One statement decides the reclaim, so two racers cannot both take
            # it: the winner is the one whose UPDATE matched a row.  The lease
            # clock and ``now()`` are the database's, so several servers cannot
            # disagree about how old a claim is.
            reclaimed = self._connection.execute(
                "UPDATE connected_sale_commands "
                "SET request_hash = %s, reserved_at = now() "
                "WHERE sale_id = %s AND status = 'pending' AND request_hash = %s "
                "AND reserved_at < now() - make_interval(secs => %s)",
                (request_hash, sale_id, request_hash, self._lease_seconds),
            )
            if reclaimed.rowcount == 1:
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
            self._lease_seconds,
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


def _lease_seconds(value: object) -> int:
    """Return a lease length in whole seconds, or refuse it.

    There is always a lease, so the length is always positive.  Holding a claim
    forever was a recorded *defect*, not a capability anyone wants, so there is
    nothing to switch back to and no ``0`` meaning anything: the naive reading
    of ``0`` is zero seconds of exclusivity, the exact opposite of what it used
    to mean, and a reservation must not depend on a reader noticing which is
    which.  Someone who wants a very long lease sets a very long number.

    This is the one place the rule lives, and both adapters already call it from
    their constructor, so a non-positive length is refused before any connection
    is opened rather than quietly disabling the reclaim.
    """

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("lease_seconds must be a positive integer")
    return value


def _replay_or_conflict(
    sale_id: str,
    existing: _Command,
    request_hash: str,
    lease_seconds: int,
) -> SaleCommandReservation:
    """Classify an existing command row against the request it was reserved for."""

    if existing.status != COMPLETED:
        # The holder's lease is the caller's wait: it is how long a live attempt
        # may legitimately take, so it is the answer to "how long until I retry".
        return SaleCommandReservation(sale_id, IN_PROGRESS, retry_after_seconds=lease_seconds)
    if existing.response is None:
        return SaleCommandReservation(sale_id, PAYLOAD_CONFLICT)
    if existing.request_hash != request_hash:
        return SaleCommandReservation(sale_id, PAYLOAD_CONFLICT)
    return SaleCommandReservation(sale_id, REPLAY, dict(existing.response))


__all__ = [
    "COMPLETED",
    "CREATE_COMMANDS_SQL",
    "DEFAULT_LEASE_SECONDS",
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
