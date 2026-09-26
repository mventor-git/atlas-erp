"""Durable local business state: the item master, sales, journals, movements.

:mod:`atlas_erp.sale_store` persists the *receipt* of a connected sale command.
This module persists the business state behind such a receipt, so the two agree
after a restart: a peer that gets a durable ``201`` replaying a ``sale_id`` finds
a sale that :meth:`~atlas_erp.business.Business.audit_snapshot` still lists and
:meth:`~atlas_erp.business.Business.stock_for` still reflects.

Two adapters implement the one :class:`BusinessStore` interface:

* :class:`InMemoryBusinessStore` for unit tests and the loopback smoke.
* :class:`PostgresBusinessStore` for cross-process durability.

Two rules make the round trip honest rather than merely plausible:

* **Order is stored, not derived.** The domain has no clock and no sequence, so
  the audit snapshot reports sales, journals, and movements in raw ``dict``
  insertion order.  Every table therefore carries a ``bigserial`` insertion
  column and ``load`` orders by it.  Ordering by a business id would be wrong:
  ``sale-audit``, ``sale-json``, ``sale-later`` are not alphabetical.
* **No derived value is stored.** Stock is a sum over movements and a journal
  total is a sum over its lines, so there is no stock column and no
  ``total_debits_cents`` column anywhere here; both are re-derived on read.  A
  stored copy would drift from the lines it was copied from.  Money is integer
  cents in a ``bigint`` and never becomes a float.

Purchasing state is not stored yet.  :class:`~atlas_erp.business.Business`
records purchase orders and receipts, and this module deliberately has no table
for either, so a reloaded domain has movements without the receipts that
produced them.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from .business import JournalEntry, JournalLine, MasterItem, Sale, SaleLine, StockMovement

if TYPE_CHECKING:
    import psycopg


# The tables this module owns.  Every name is a literal here on purpose: no
# caller-supplied value ever reaches a table or column name.  ``seq`` is the
# insertion order the domain has no other way to express.
CREATE_ITEMS_SQL = """
CREATE TABLE IF NOT EXISTS master_items (
    seq bigserial PRIMARY KEY,
    item_id text NOT NULL UNIQUE,
    sku text NOT NULL UNIQUE,
    name text NOT NULL,
    price_cents bigint NOT NULL
)
"""

CREATE_SALES_SQL = """
CREATE TABLE IF NOT EXISTS sales (
    seq bigserial PRIMARY KEY,
    sale_id text NOT NULL UNIQUE,
    customer_id text NOT NULL,
    journal_id text NOT NULL
);

CREATE TABLE IF NOT EXISTS sale_lines (
    sale_id text NOT NULL,
    line_index integer NOT NULL,
    item_id text NOT NULL,
    quantity bigint NOT NULL,
    unit_price_cents bigint NOT NULL,
    PRIMARY KEY (sale_id, line_index)
)
"""

CREATE_JOURNALS_SQL = """
CREATE TABLE IF NOT EXISTS journal_entries (
    seq bigserial PRIMARY KEY,
    journal_id text NOT NULL UNIQUE,
    sale_id text,
    state text NOT NULL
);

CREATE TABLE IF NOT EXISTS journal_lines (
    journal_id text NOT NULL,
    line_index integer NOT NULL,
    account_code text NOT NULL,
    debit_cents bigint NOT NULL,
    credit_cents bigint NOT NULL,
    PRIMARY KEY (journal_id, line_index)
)
"""

CREATE_MOVEMENTS_SQL = """
CREATE TABLE IF NOT EXISTS stock_movements (
    seq bigserial PRIMARY KEY,
    movement_id text NOT NULL UNIQUE,
    item_id text NOT NULL,
    quantity_delta bigint NOT NULL,
    reason text NOT NULL,
    reference_id text NOT NULL
)
"""

CREATE_SCHEMA_SQL = (
    CREATE_ITEMS_SQL,
    CREATE_SALES_SQL,
    CREATE_JOURNALS_SQL,
    CREATE_MOVEMENTS_SQL,
)


@dataclass(frozen=True)
class BusinessRecords:
    """The stored business state a :class:`BusinessStore` can return.

    The field names are the keyword arguments of
    :meth:`~atlas_erp.business.Business.restore`, so a load result can be
    handed to the domain directly.
    """

    items: tuple[MasterItem, ...] = ()
    sales: tuple[Sale, ...] = ()
    journals: tuple[JournalEntry, ...] = ()
    stock_movements: tuple[StockMovement, ...] = ()


class BusinessStore(Protocol):
    """Durable local business state, as records rather than as stock levels.

    :meth:`load` returns what was stored, in the order it was stored.
    :meth:`save_item` writes one item, and :meth:`save_sale` writes one sale
    with its journal while :meth:`save_movements` writes the movement lines.
    Nothing here validates: a record is written as the domain produced it, and
    a duplicate key is a caller error the adapter's own error reports.

    Known ceiling: writes are one transaction per call, so a sale, its journal,
    and its movements are not atomic with each other.  Merging them with the
    sale receipt into one transaction is deliberately left to a later slice.
    """

    def ensure_schema(self) -> None:
        """Create the owned tables when they do not exist yet."""

        ...

    def load(self) -> BusinessRecords:
        """Return every stored record, in insertion order."""

        ...

    def save_item(self, item: MasterItem) -> None:
        """Store one master item."""

        ...

    def save_sale(self, sale: Sale, journal: JournalEntry) -> None:
        """Store one sale, its lines, and the journal entry it posted."""

        ...

    def save_movements(self, movements: Iterable[StockMovement]) -> None:
        """Store stock movements, in the order they are given."""

        ...

    def close(self) -> None:
        """Release any resource the store holds."""

        ...


class InMemoryBusinessStore:
    """A process-local business store, used by tests and the loopback smoke.

    It gives the same round trip as the PostgreSQL adapter within one process,
    and nothing more: the state is lost on restart, exactly as the in-memory
    domain is.
    """

    def __init__(self) -> None:
        self._items: dict[str, MasterItem] = {}
        self._sales: dict[str, Sale] = {}
        self._journals: dict[str, JournalEntry] = {}
        self._movements: dict[str, StockMovement] = {}
        self._lock = threading.Lock()

    def ensure_schema(self) -> None:
        return None

    def load(self) -> BusinessRecords:
        with self._lock:
            return BusinessRecords(
                items=tuple(self._items.values()),
                sales=tuple(self._sales.values()),
                journals=tuple(self._journals.values()),
                stock_movements=tuple(self._movements.values()),
            )

    def save_item(self, item: MasterItem) -> None:
        with self._lock:
            self._items[item.item_id] = item

    def save_sale(self, sale: Sale, journal: JournalEntry) -> None:
        with self._lock:
            self._sales[sale.sale_id] = sale
            self._journals[journal.journal_id] = journal

    def save_movements(self, movements: Iterable[StockMovement]) -> None:
        with self._lock:
            for movement in movements:
                self._movements[movement.movement_id] = movement

    def close(self) -> None:
        return None


class PostgresBusinessStore:
    """A durable business store backed by six PostgreSQL tables.

    One connection is held for the life of the store and every method takes a
    lock and a transaction, so one instance is safe to share between the
    serving thread and a caller in another thread.
    """

    def __init__(self, dsn: str, *, connect_timeout: int = 5) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("database URL must be a non-empty string")
        self._lock = threading.Lock()
        self._connection = _connect(dsn, connect_timeout=connect_timeout)
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create the owned tables when they do not exist yet."""

        with self._lock, self._connection.transaction():
            for statement in CREATE_SCHEMA_SQL:
                self._connection.execute(statement)

    def load(self) -> BusinessRecords:
        with self._lock, self._connection.transaction():
            items = tuple(
                MasterItem(row[0], row[1], row[2], row[3])
                for row in self._connection.execute(
                    "SELECT item_id, sku, name, price_cents FROM master_items ORDER BY seq"
                )
            )
            # Lines are grouped in Python rather than by a joined row stream,
            # because the order that matters is the sale's own ``seq`` order and
            # a join would interleave the two.
            sale_lines: dict[str, list[SaleLine]] = {}
            for sale_id, item_id, quantity, unit_price_cents in self._connection.execute(
                "SELECT sale_id, item_id, quantity, unit_price_cents FROM sale_lines "
                "ORDER BY sale_id, line_index"
            ):
                sale_lines.setdefault(str(sale_id), []).append(
                    SaleLine(str(item_id), quantity, unit_price_cents)
                )
            sales = tuple(
                Sale(row[0], row[1], tuple(sale_lines.get(str(row[0]), ())), row[2])
                for row in self._connection.execute(
                    "SELECT sale_id, customer_id, journal_id FROM sales ORDER BY seq"
                )
            )
            journal_lines: dict[str, list[JournalLine]] = {}
            for journal_id, account_code, debit_cents, credit_cents in (
                self._connection.execute(
                    "SELECT journal_id, account_code, debit_cents, credit_cents "
                    "FROM journal_lines ORDER BY journal_id, line_index"
                )
            ):
                journal_lines.setdefault(str(journal_id), []).append(
                    JournalLine(account_code, debit_cents, credit_cents)
                )
            journals = tuple(
                JournalEntry(
                    row[0], row[1], tuple(journal_lines.get(str(row[0]), ())), row[2]
                )
                for row in self._connection.execute(
                    "SELECT journal_id, sale_id, state FROM journal_entries ORDER BY seq"
                )
            )
            stock_movements = tuple(
                StockMovement(row[0], row[1], row[2], row[3], row[4])
                for row in self._connection.execute(
                    "SELECT movement_id, item_id, quantity_delta, reason, reference_id "
                    "FROM stock_movements ORDER BY seq"
                )
            )
        return BusinessRecords(items, sales, journals, stock_movements)

    def save_item(self, item: MasterItem) -> None:
        with self._lock, self._connection.transaction():
            self._connection.execute(
                "INSERT INTO master_items (item_id, sku, name, price_cents) "
                "VALUES (%s, %s, %s, %s)",
                (item.item_id, item.sku, item.name, item.price_cents),
            )

    def save_sale(self, sale: Sale, journal: JournalEntry) -> None:
        with self._lock, self._connection.transaction():
            self._connection.execute(
                "INSERT INTO sales (sale_id, customer_id, journal_id) VALUES (%s, %s, %s)",
                (sale.sale_id, sale.customer_id, sale.journal_id),
            )
            for index, line in enumerate(sale.lines):
                self._connection.execute(
                    "INSERT INTO sale_lines "
                    "(sale_id, line_index, item_id, quantity, unit_price_cents) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (sale.sale_id, index, line.item_id, line.quantity, line.unit_price_cents),
                )
            self._connection.execute(
                "INSERT INTO journal_entries (journal_id, sale_id, state) "
                "VALUES (%s, %s, %s)",
                (journal.journal_id, journal.sale_id, journal.state),
            )
            for index, line in enumerate(journal.lines):
                self._connection.execute(
                    "INSERT INTO journal_lines "
                    "(journal_id, line_index, account_code, debit_cents, credit_cents) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        journal.journal_id,
                        index,
                        line.account_code,
                        line.debit_cents,
                        line.credit_cents,
                    ),
                )

    def save_movements(self, movements: Iterable[StockMovement]) -> None:
        with self._lock, self._connection.transaction():
            for movement in movements:
                self._connection.execute(
                    "INSERT INTO stock_movements "
                    "(movement_id, item_id, quantity_delta, reason, reference_id) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        movement.movement_id,
                        movement.item_id,
                        movement.quantity_delta,
                        movement.reason,
                        movement.reference_id,
                    ),
                )

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _connect(dsn: str, *, connect_timeout: int) -> psycopg.Connection[Any]:
    """Open one connection, importing the driver only when it is needed.

    The import is local so the package keeps working when the optional
    PostgreSQL driver is not installed.  Rows are read positionally, in the
    column order of the statements above.
    """

    import psycopg

    return psycopg.connect(dsn, connect_timeout=connect_timeout)


__all__ = [
    "BusinessRecords",
    "BusinessStore",
    "CREATE_ITEMS_SQL",
    "CREATE_JOURNALS_SQL",
    "CREATE_MOVEMENTS_SQL",
    "CREATE_SALES_SQL",
    "CREATE_SCHEMA_SQL",
    "InMemoryBusinessStore",
    "PostgresBusinessStore",
]
