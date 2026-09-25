"""Tests for the connected sale receipt store.

The in-memory tests run everywhere.  The PostgreSQL tests run only against the
real temporary instance named by ``ATLAS_ERP_DATABASE_URL``, because a fake
would not prove that a receipt survives closing and reopening the store.
"""

from __future__ import annotations

import os
import unittest
from uuid import uuid4

from atlas_erp import (
    IN_PROGRESS,
    PAYLOAD_CONFLICT,
    REPLAY,
    RESERVED,
    InMemorySaleCommandStore,
    PostgresSaleCommandStore,
    SaleCommandError,
)

DATABASE_ENV = "ATLAS_ERP_DATABASE_URL"
RECEIPT = {"app_id": "atlas-erp", "sale_id": "sale-store-1", "total_cents": 1250}


def _database_url() -> str:
    return os.environ.get(DATABASE_ENV, "")


class InMemorySaleCommandStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemorySaleCommandStore()

    def test_a_completed_command_replays_and_another_request_conflicts(self) -> None:
        self.assertEqual(self.store.reserve("sale-1", "hash-a").outcome, RESERVED)
        self.assertEqual(self.store.reserve("sale-1", "hash-a").outcome, IN_PROGRESS)

        self.store.complete("sale-1", RECEIPT)

        replay = self.store.reserve("sale-1", "hash-a")
        self.assertEqual(replay.outcome, REPLAY)
        self.assertEqual(replay.response, RECEIPT)
        self.assertEqual(
            self.store.reserve("sale-1", "hash-b").outcome, PAYLOAD_CONFLICT
        )

    def test_a_failed_command_releases_its_key_and_keeps_accepted_receipts(self) -> None:
        self.assertEqual(self.store.reserve("sale-2", "hash-a").outcome, RESERVED)
        self.store.abort("sale-2")
        self.assertEqual(self.store.reserve("sale-2", "hash-a").outcome, RESERVED)

        self.store.complete("sale-2", RECEIPT)
        self.store.abort("sale-2")
        self.assertEqual(self.store.reserve("sale-2", "hash-a").outcome, REPLAY)

    def test_completing_a_command_that_is_not_reserved_is_an_error(self) -> None:
        with self.assertRaises(SaleCommandError):
            self.store.complete("sale-missing", RECEIPT)

        self.store.reserve("sale-3", "hash-a")
        self.store.complete("sale-3", RECEIPT)
        with self.assertRaises(SaleCommandError):
            self.store.complete("sale-3", RECEIPT)

    def test_close_may_be_called_more_than_once(self) -> None:
        self.store.close()
        self.store.close()


@unittest.skipUnless(_database_url(), f"set {DATABASE_ENV} to run")
class PostgresSaleCommandStoreTests(unittest.TestCase):
    """Durability tests against the real temporary PostgreSQL instance."""

    def setUp(self) -> None:
        # A private sale_id per run keeps a shared instance clean and keeps
        # leftovers from an earlier run from failing these tests.
        self.sale_ids = [f"sale-store-{uuid4().hex}" for _ in range(3)]
        self.addCleanup(self._delete_own_rows)

    def _delete_own_rows(self) -> None:
        import psycopg

        with psycopg.connect(_database_url()) as connection:
            connection.execute(
                "DELETE FROM connected_sale_commands WHERE sale_id = ANY(%s)",
                (self.sale_ids,),
            )

    def open_store(self) -> PostgresSaleCommandStore:
        store = PostgresSaleCommandStore(_database_url())
        self.addCleanup(store.close)
        return store

    def test_a_completed_receipt_replays_after_a_reopen(self) -> None:
        sale_id = self.sale_ids[0]
        first = self.open_store()
        self.assertEqual(first.reserve(sale_id, "hash-a").outcome, RESERVED)
        first.complete(sale_id, RECEIPT)
        first.close()

        # A second store stands in for a restarted server process.
        second = self.open_store()
        replay = second.reserve(sale_id, "hash-a")
        self.assertEqual(replay.outcome, REPLAY)
        self.assertEqual(replay.response, RECEIPT)
        self.assertEqual(
            second.reserve(sale_id, "hash-b").outcome, PAYLOAD_CONFLICT
        )

    def test_only_one_instance_holds_a_sale_id_at_a_time(self) -> None:
        sale_id = self.sale_ids[1]
        first = self.open_store()
        second = self.open_store()

        self.assertEqual(first.reserve(sale_id, "hash-a").outcome, RESERVED)
        self.assertEqual(second.reserve(sale_id, "hash-a").outcome, IN_PROGRESS)
        first.abort(sale_id)
        self.assertEqual(second.reserve(sale_id, "hash-a").outcome, RESERVED)

    def test_a_completed_receipt_survives_a_later_abort(self) -> None:
        sale_id = self.sale_ids[2]
        store = self.open_store()

        store.reserve(sale_id, "hash-a")
        store.complete(sale_id, RECEIPT)
        store.abort(sale_id)

        self.assertEqual(store.reserve(sale_id, "hash-a").outcome, REPLAY)


if __name__ == "__main__":
    unittest.main()
