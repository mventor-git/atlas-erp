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
)
# A derived value would be a second source of truth, so none may be stored.
DERIVED_COLUMNS = frozenset(
    {"stock", "total_cents", "total_debits_cents", "total_credits_cents"}
)
# Posted in this order, which no id sort reproduces.
RECORDED_SALE_SUFFIXES = ("later", "audit", "json")


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
        order = business.create_purchase_order(
            "supplier-1",
            [(first.item_id, 4), (second.item_id, 6)],
            order_id=f"{self.prefix}-po-1",
        )
        business.receive_purchase_order(order.order_id, f"{self.prefix}-receipt-1")
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
    case.assertEqual(restored.stock_for(f"{round_trip.prefix}-item-2"), 5)


def _assert_recorded_order_is_returned(
    case: unittest.TestCase, round_trip: BusinessStoreRoundTrip
) -> None:
    business = round_trip.build_business()
    store = round_trip.open_store()
    case.addCleanup(store.close)
    _seed_business_store(store, business)

    records = store.load()

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
        self.assertEqual(records.sales, ())
        self.assertEqual(records.journals, ())
        self.assertEqual(records.stock_movements, ())

    def test_ensure_schema_and_close_may_be_called_more_than_once(self) -> None:
        store = self.open_store()

        store.ensure_schema()
        store.ensure_schema()
        store.close()
        store.close()


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
                (f"{self.prefix}%",),
            )
            connection.execute(
                "DELETE FROM master_items WHERE item_id LIKE %s", (f"{self.prefix}%",)
            )

    def open_store(self) -> BusinessStore:
        store = PostgresBusinessStore(_database_url())
        self.addCleanup(store.close)
        return store

    def test_a_sale_survives_a_reopen_and_reloads_byte_identically(self) -> None:
        # A second PostgresBusinessStore is a real restart here: the first
        # instance is closed and its connection dropped before this one exists.
        _assert_a_sale_survives_a_reopen(self, self)

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
