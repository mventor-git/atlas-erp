"""Tests for the durable local business store.

The in-memory tests run everywhere.  The PostgreSQL tests run only against the
real temporary instance named by ``ATLAS_ERP_DATABASE_URL``, because a fake
would not prove that a sale survives closing and reopening the store.

The round trip is defined once and run against both adapters: a sale written
through a store is still a sale after that store is closed, reopened, and
reloaded into a fresh :class:`~atlas_erp.business.Business`, with the audit
projection unchanged right down to the cursor.
"""

from __future__ import annotations

import os
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

from atlas_erp import (
    Business,
    BusinessStore,
    InMemoryBusinessStore,
    PostgresBusinessStore,
    snapshot_cursor,
)
from atlas_erp.connect_server import _seed_business_store

DATABASE_ENV = "ATLAS_ERP_DATABASE_URL"
# The tables the store owns.  A test reads them; it never creates them.
OWNED_TABLES = (
    "master_items",
    "sales",
    "sale_lines",
    "journal_entries",
    "journal_lines",
    "stock_movements",
    "purchase_orders",
    "purchase_order_lines",
    "receipts",
    "receipt_lines",
)
# A derived value would be a second source of truth, so none may be stored.
DERIVED_COLUMNS = frozenset(
    {
        "stock",
        "total_cents",
        "total_debits_cents",
        "total_credits_cents",
        # is_received is ``state == "received"``, so a column for it would be a
        # copy of a comparison that could disagree with the state beside it.
        "is_received",
    }
)
# Posted in this order, which no id sort reproduces.
RECORDED_SALE_SUFFIXES = ("later", "audit", "json")
# The same for purchasing: two received orders, in an order no id sort
# reproduces, followed by one order left open, which has no receipt.
RECORDED_PURCHASING_SUFFIXES = ("json", "audit", "open")
RECORDED_RECEIPT_SUFFIXES = ("json", "audit")
# The movement reason a receipt produced, which is what a movement's
# reference_id has to resolve to after a reload.
RECEIPT_REASON = "receipt"


def _database_url() -> str:
    return os.environ.get(DATABASE_ENV, "")


class BusinessStoreRoundTrip:
    """The fixtures both adapters share, without any assertion of its own.

    This is a plain mixin rather than a base :class:`unittest.TestCase` so
    discovery collects only the concrete adapters and runs the shared checks
    once per adapter.
    """

    def setUp(self) -> None:
        # A private id prefix per run keeps a shared instance clean and keeps
        # leftovers from an earlier run from failing these tests.
        self.prefix = f"bs-{uuid4().hex}"

    def open_store(self) -> BusinessStore:
        raise NotImplementedError

    def reopen(self, store: BusinessStore) -> BusinessStore:
        """A second store over the same state, standing in for a restart.

        Only an adapter whose state outlives the instance may answer this with a
        new instance; the in-memory one has to be asked again instead, which is
        the honest difference between them.
        """

        return self.open_store()

    def build_business(self) -> Business:
        """Post a domain whose recorded order is not its alphabetical order."""

        business = Business()
        first = business.register_item(
            f"{self.prefix}-item-1", f"{self.prefix}-SKU-1", "First", 1250
        )
        second = business.register_item(
            f"{self.prefix}-item-2", f"{self.prefix}-SKU-2", "Second", 500
        )
        # Two received orders, in an order no id sort reproduces, and both with
        # more than one line so the stored line order is part of the round trip.
        for suffix, lines in (
            ("json", [(first.item_id, 4), (second.item_id, 2)]),
            ("audit", [(first.item_id, 3), (second.item_id, 6)]),
        ):
            order = business.create_purchase_order(
                "supplier-1",
                lines,
                order_id=f"{self.prefix}-po-{suffix}",
            )
            business.receive_purchase_order(
                order.order_id, f"{self.prefix}-receipt-{suffix}"
            )
        # One order left open, because an unreceived order is the only row whose
        # receipt_id is NULL and a receipt-less order is still purchasing state.
        business.create_purchase_order(
            "supplier-2",
            [(first.item_id, 1)],
            order_id=f"{self.prefix}-po-open",
        )
        for suffix, customer, item, price in (
            ("later", "customer-1", first, 1250),
            ("audit", "customer-2", second, 500),
            ("json", "customer-3", first, 1250),
        ):
            business.create_manual_sale(
                customer,
                [{"item_id": item.item_id, "quantity": 1, "unit_price_cents": price}],
                sale_id=f"{self.prefix}-sale-{suffix}",
            )
        return business

    def load_business(self, store: BusinessStore) -> Business:
        records = store.load()
        business = Business()
        business.restore(
            items=records.items,
            purchase_orders=records.purchase_orders,
            receipts=records.receipts,
            sales=records.sales,
            journals=records.journals,
            stock_movements=records.stock_movements,
        )
        return business


def _assert_a_sale_survives_a_reopen(
    case: unittest.TestCase, round_trip: BusinessStoreRoundTrip
) -> None:
    business = round_trip.build_business()
    store = round_trip.open_store()
    case.addCleanup(store.close)
    _seed_business_store(store, business)
    before = business.audit_snapshot().to_dict()
    store.close()

    reopened = round_trip.reopen(store)
    case.addCleanup(reopened.close)
    restored = round_trip.load_business(reopened)

    after = restored.audit_snapshot().to_dict()
    case.assertEqual(after, before)
    # The cursor is the SHA-256 of the canonical JSON of the projection, so an
    # equal cursor is a byte-identical payload and not merely equal data.
    case.assertEqual(snapshot_cursor(after), snapshot_cursor(before))
    # The sale a replayed receipt names is still a sale, and stock reflects it.
    case.assertEqual(
        list(restored.sales),
        [f"{round_trip.prefix}-sale-{suffix}" for suffix in RECORDED_SALE_SUFFIXES],
    )
    case.assertEqual(restored.stock, business.stock)
    journal = restored.get_journal_for_sale(f"{round_trip.prefix}-sale-audit")
    case.assertEqual(journal.total_cents, 500)
    case.assertEqual(journal.total_debits_cents, journal.total_credits_cents)
    # item-2 was received 2 + 6 and sold 1.
    case.assertEqual(restored.stock_for(f"{round_trip.prefix}-item-2"), 7)


def _assert_purchasing_survives_a_reopen(
    case: unittest.TestCase, round_trip: BusinessStoreRoundTrip
) -> None:
    """The order and the receipt are records again, not lost movements."""

    business = round_trip.build_business()
    store = round_trip.open_store()
    case.addCleanup(store.close)
    _seed_business_store(store, business)
    before = business.audit_snapshot().to_dict()
    store.close()

    reopened = round_trip.reopen(store)
    case.addCleanup(reopened.close)
    records = reopened.load()
    restored = round_trip.load_business(reopened)

    # Equality of the whole records, so a lost or reordered line fails here.
    case.assertEqual(records.purchase_orders, tuple(business.purchase_orders.values()))
    case.assertEqual(records.receipts, tuple(business.receipts.values()))
    # state and receipt_id are stored, so the order is still a received one.
    order = restored.get_purchase_order(f"{round_trip.prefix}-po-json")
    case.assertTrue(order.is_received)
    case.assertEqual(order.receipt_id, f"{round_trip.prefix}-receipt-json")
    # An order nobody received comes back open, with no receipt: the NULL
    # receipt_id round-trips as None rather than as an empty reference.
    open_order = restored.get_purchase_order(f"{round_trip.prefix}-po-open")
    case.assertFalse(open_order.is_received)
    case.assertIsNone(open_order.receipt_id)
    case.assertIsNone(restored.get_receipt_for_order(f"{round_trip.prefix}-po-open"))
    # The derived index is rebuilt on reload.  This is the assertion the missing
    # purchasing tables would have failed: without _receipts_by_order the
    # lookup answers None even though the receipt itself came back.
    case.assertEqual(
        restored.get_receipt_for_order(f"{round_trip.prefix}-po-audit"),
        business.get_receipt_for_order(f"{round_trip.prefix}-po-audit"),
    )
    # The movements a receipt produced came back beside it, and the projection
    # the audit route serves is byte-identical, cursor included.
    case.assertEqual(
        [movement.movement_id for movement in records.stock_movements],
        list(business.stock_movements),
    )
    after = restored.audit_snapshot().to_dict()
    case.assertEqual(after, before)
    case.assertEqual(snapshot_cursor(after), snapshot_cursor(before))


def _assert_no_reference_dangles_after_a_reopen(
    case: unittest.TestCase, round_trip: BusinessStoreRoundTrip
) -> None:
    """Every stored reference resolves, in both directions.

    A movement that names a receipt which does not exist, or a receipt that
    names an order which does not, is the incoherence this slice exists to
    close: ``stock_for`` stays right either way, so only a reference check
    can see it.
    """

    business = round_trip.build_business()
    store = round_trip.open_store()
    case.addCleanup(store.close)
    _seed_business_store(store, business)
    store.close()

    reopened = round_trip.reopen(store)
    case.addCleanup(reopened.close)
    restored = round_trip.load_business(reopened)

    receipt_movements = [
        movement
        for movement in restored.stock_movements.values()
        if movement.reason == RECEIPT_REASON
    ]
    case.assertTrue(receipt_movements, "the fixture must produce receipt movements")
    for movement in receipt_movements:
        with case.subTest(movement=movement.movement_id):
            case.assertIn(movement.reference_id, restored.receipts)
    for receipt in restored.receipts.values():
        with case.subTest(receipt=receipt.receipt_id):
            case.assertIn(receipt.order_id, restored.purchase_orders)
    # The other direction: a receipt nothing moved, or an order marked received
    # with no receipt, is purchasing state the stock does not reflect.
    case.assertEqual(
        {movement.reference_id for movement in receipt_movements},
        set(restored.receipts),
    )
    case.assertEqual(
        {receipt.order_id for receipt in restored.receipts.values()},
        {
            order.order_id
            for order in restored.purchase_orders.values()
            if order.is_received
        },
    )


def _assert_recorded_order_is_returned(
    case: unittest.TestCase, round_trip: BusinessStoreRoundTrip
) -> None:
    business = round_trip.build_business()
    store = round_trip.open_store()
    case.addCleanup(store.close)
    _seed_business_store(store, business)

    records = store.load()

    case.assertEqual(
        [order.order_id for order in records.purchase_orders],
        [
            f"{round_trip.prefix}-po-{suffix}"
            for suffix in RECORDED_PURCHASING_SUFFIXES
        ],
    )
    case.assertEqual(
        [receipt.receipt_id for receipt in records.receipts],
        [
            f"{round_trip.prefix}-receipt-{suffix}"
            for suffix in RECORDED_RECEIPT_SUFFIXES
        ],
    )
    case.assertEqual(
        [sale.sale_id for sale in records.sales],
        [f"{round_trip.prefix}-sale-{suffix}" for suffix in RECORDED_SALE_SUFFIXES],
    )
    case.assertEqual(
        [journal.journal_id for journal in records.journals],
        [f"journal-{round_trip.prefix}-sale-{suffix}" for suffix in RECORDED_SALE_SUFFIXES],
    )
    case.assertEqual(
        [movement.movement_id for movement in records.stock_movements],
        list(business.stock_movements),
    )
    case.assertEqual(records.sales, tuple(business.sales.values()))
    case.assertEqual(records.journals, tuple(business.journals.values()))


class InMemoryBusinessStoreTests(BusinessStoreRoundTrip, unittest.TestCase):
    def open_store(self) -> BusinessStore:
        return InMemoryBusinessStore()

    def reopen(self, store: BusinessStore) -> BusinessStore:
        # Nothing survives outside the instance, so "a restart" can only mean
        # asking the same store again.  The ceiling is pinned below.
        return store

    def test_a_sale_written_through_the_store_reloads_byte_identically(self) -> None:
        _assert_a_sale_survives_a_reopen(self, self)

    def test_purchasing_survives_a_reopen(self) -> None:
        _assert_purchasing_survives_a_reopen(self, self)

    def test_no_reference_dangles_after_a_reopen(self) -> None:
        _assert_no_reference_dangles_after_a_reopen(self, self)

    def test_recorded_order_is_returned_rather_than_id_order(self) -> None:
        _assert_recorded_order_is_returned(self, self)

    def test_a_second_instance_starts_empty(self) -> None:
        store = self.open_store()
        _seed_business_store(store, self.build_business())

        self.assertEqual(self.open_store().load().items, ())

    def test_an_empty_store_loads_no_records(self) -> None:
        store = self.open_store()

        store.ensure_schema()
        records = store.load()

        self.assertEqual(records.items, ())
        self.assertEqual(records.purchase_orders, ())
        self.assertEqual(records.receipts, ())
        self.assertEqual(records.sales, ())
        self.assertEqual(records.journals, ())
        self.assertEqual(records.stock_movements, ())

    def test_the_seed_writes_everything_through_one_transaction(self) -> None:
        # The in-memory transaction cannot roll anything back, so what is
        # pinned here is only that the seed opens one at all: the next boot
        # trusts whatever is there, so the writes must be a single unit.
        store = _TransactionCountingStore()

        _seed_business_store(store, self.build_business())

        self.assertEqual(store.transactions, 1)
        self.assertEqual(len(store.load().purchase_orders), 3)
        self.assertEqual(len(store.load().receipts), 2)

    def test_ensure_schema_and_close_may_be_called_more_than_once(self) -> None:
        store = self.open_store()

        store.ensure_schema()
        store.ensure_schema()
        store.close()
        store.close()


class _TransactionCountingStore(InMemoryBusinessStore):
    """An in-memory store that counts the transaction blocks the seed opens."""

    def __init__(self) -> None:
        super().__init__()
        self.transactions = 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.transactions += 1
        with super().transaction():
            yield None


class _RefusingReceiptStore(PostgresBusinessStore):
    """A durable store whose receipt write fails, to pin the seed's boundary."""

    def save_receipt(self, receipt: object) -> None:
        raise RuntimeError("the receipt write was refused")


@unittest.skipUnless(_database_url(), f"set {DATABASE_ENV} to run")
class PostgresBusinessStoreTests(BusinessStoreRoundTrip, unittest.TestCase):
    """Durability tests against the real temporary PostgreSQL instance."""

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(self._delete_own_rows)

    def _delete_own_rows(self) -> None:
        import psycopg

        journal_ids = [
            f"journal-{self.prefix}-sale-{suffix}" for suffix in RECORDED_SALE_SUFFIXES
        ]
        with psycopg.connect(_database_url()) as connection:
            # The line tables carry no foreign key, so they go by id first.
            connection.execute(
                "DELETE FROM sale_lines WHERE sale_id LIKE %s", (f"{self.prefix}%",)
            )
            connection.execute(
                "DELETE FROM journal_lines WHERE journal_id = ANY(%s)", (journal_ids,)
            )
            connection.execute(
                "DELETE FROM sales WHERE sale_id LIKE %s", (f"{self.prefix}%",)
            )
            connection.execute(
                "DELETE FROM journal_entries WHERE journal_id = ANY(%s)", (journal_ids,)
            )
            connection.execute(
                "DELETE FROM stock_movements WHERE reference_id LIKE %s",
                (f"{self.prefix}%",)
            )
            connection.execute(
                "DELETE FROM master_items WHERE item_id LIKE %s", (f"{self.prefix}%",)
            )
            connection.execute(
                "DELETE FROM purchase_order_lines WHERE order_id LIKE %s",
                (f"{self.prefix}%",),
            )
            connection.execute(
                "DELETE FROM receipt_lines WHERE receipt_id LIKE %s",
                (f"{self.prefix}%",),
            )
            connection.execute(
                "DELETE FROM receipts WHERE receipt_id LIKE %s", (f"{self.prefix}%",)
            )
            connection.execute(
                "DELETE FROM purchase_orders WHERE order_id LIKE %s",
                (f"{self.prefix}%",),
            )

    def open_store(self) -> BusinessStore:
        store = PostgresBusinessStore(_database_url())
        self.addCleanup(store.close)
        return store

    def test_a_sale_survives_a_reopen_and_reloads_byte_identically(self) -> None:
        # A second PostgresBusinessStore is a real restart here: the first
        # instance is closed and its connection dropped before this one exists.
        _assert_a_sale_survives_a_reopen(self, self)

    def test_purchasing_survives_a_reopen(self) -> None:
        # A purchase order and its receipt come back after the store is closed
        # and reopened, the movements still match, and the projection the audit
        # route serves is byte-identical right down to the cursor.
        _assert_purchasing_survives_a_reopen(self, self)

    def test_no_reference_dangles_after_a_reopen(self) -> None:
        _assert_no_reference_dangles_after_a_reopen(self, self)

    def test_a_refused_write_rolls_the_whole_seed_back(self) -> None:
        # The seed is one unit of work because the next boot restores whatever
        # is there as truth: a partial store would be a lie, not a slow start.
        store = self.open_store()
        refusing = _RefusingReceiptStore(_database_url())
        self.addCleanup(refusing.close)

        with self.assertRaises(RuntimeError):
            _seed_business_store(refusing, self.build_business())

        self.assertEqual(store.load().items, ())
        self.assertEqual(store.load().purchase_orders, ())
        self.assertEqual(store.load().receipts, ())
        self.assertEqual(store.load().stock_movements, ())

    def test_recorded_order_is_returned_rather_than_id_order(self) -> None:
        _assert_recorded_order_is_returned(self, self)

    def test_a_second_instance_reads_what_the_first_one_committed(self) -> None:
        business = self.build_business()
        first = self.open_store()
        # Opened before the first one writes, so this can only be a live read of
        # committed rows and not of an in-process cache.
        second = self.open_store()

        self.assertEqual(len(second.load().sales), 0)
        _seed_business_store(first, business)
        self.assertEqual(len(second.load().sales), len(business.sales))

    def test_the_schema_stores_records_rather_than_derived_values(self) -> None:
        import psycopg

        self.open_store()

        with psycopg.connect(_database_url()) as connection:
            rows = connection.execute(
                "SELECT table_name, column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = ANY(%s)",
                (list(OWNED_TABLES),),
            ).fetchall()

        stored = {(str(row[0]), str(row[1])): str(row[2]) for row in rows}
        self.assertEqual(sorted({table for table, _ in stored}), sorted(OWNED_TABLES))
        for table, column in stored:
            with self.subTest(table=table, column=column):
                self.assertNotIn(column, DERIVED_COLUMNS)
        # Money is integer cents, so no money column is a float or a numeric.
        for (table, column), data_type in stored.items():
            if column.endswith("_cents"):
                with self.subTest(table=table, column=column):
                    self.assertEqual(data_type, "bigint")


if __name__ == "__main__":
    unittest.main()
