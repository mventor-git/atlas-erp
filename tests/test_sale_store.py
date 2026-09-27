"""Tests for the connected sale receipt store.

The in-memory tests run everywhere.  The PostgreSQL tests run only against the
real temporary instance named by ``ATLAS_ERP_DATABASE_URL``, because a fake
would not prove that a receipt survives closing and reopening the store.
"""

from __future__ import annotations

import os
import threading
import time
import unittest
from typing import cast
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
from atlas_erp.sale_store import DEFAULT_LEASE_SECONDS

DATABASE_ENV = "ATLAS_ERP_DATABASE_URL"
RECEIPT = {"app_id": "atlas-erp", "sale_id": "sale-store-1", "total_cents": 1250}
# The in-memory lease reads a real clock, so an expired claim is reached with a
# short lease and one short wait rather than a thirty-second sleep.  It only has
# to outlast the lease; a slower machine makes the assertion more true, not
# less, so this cannot flake.
EXPIRED_LEASE_SECONDS = 1
EXPIRED_LEASE_WAIT_SECONDS = 1.05


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


class InMemorySaleCommandStoreLeaseTests(unittest.TestCase):
    """A crashed attempt must not hold a ``sale_id`` for good, and a live one must.

    The whole lease is one timeline, so it is walked once: a claim inside its
    lease is refused, a claim past it is taken over, and a completed receipt is
    refused however old it is.
    """

    def test_only_a_pending_claim_past_its_lease_is_taken_over(self) -> None:
        store = InMemorySaleCommandStore(lease_seconds=EXPIRED_LEASE_SECONDS)

        # Inside the lease: a second attempt for the same key is told the first
        # one still owns it, and how long that may legitimately take.
        self.assertEqual(store.reserve("sale-lease-1", "hash-a").outcome, RESERVED)
        held = store.reserve("sale-lease-1", "hash-a")
        self.assertEqual(held.outcome, IN_PROGRESS)
        self.assertEqual(held.retry_after_seconds, EXPIRED_LEASE_SECONDS)

        # A completed receipt is never reclaimable, whatever its age, so it is
        # aged alongside the pending claim to make the two comparable.
        self.assertEqual(store.reserve("sale-lease-2", "hash-a").outcome, RESERVED)
        store.complete("sale-lease-2", RECEIPT)

        time.sleep(EXPIRED_LEASE_WAIT_SECONDS)

        # Past the lease the crashed claim is reclaimed, and the reclaim is the
        # answer: RESERVED, not a replay of a sale that never happened.
        reclaimed = store.reserve("sale-lease-1", "hash-a")
        self.assertEqual(reclaimed.outcome, RESERVED)
        self.assertIsNone(reclaimed.response)

        # The same age buys nothing against a receipt that really was stored.
        replayed = store.reserve("sale-lease-2", "hash-a")
        self.assertEqual(replayed.outcome, REPLAY)
        self.assertEqual(replayed.response, RECEIPT)

        # And abort is still the manual release, after a reclaim as much as
        # before one: it deletes the pending row and nothing else.
        store.abort("sale-lease-1")
        self.assertEqual(store.reserve("sale-lease-1", "hash-a").outcome, RESERVED)
        store.abort("sale-lease-2")
        self.assertEqual(store.reserve("sale-lease-2", "hash-a").outcome, REPLAY)

    def test_in_progress_reports_the_default_lease_as_the_caller_s_wait(self) -> None:
        store = InMemorySaleCommandStore()

        store.reserve("sale-lease-3", "hash-a")
        blocked = store.reserve("sale-lease-3", "hash-a")

        # The wait is a number, not a shrug: without it a caller cannot tell a
        # busy key from a stuck one and has to guess when to come back.
        self.assertEqual(blocked.outcome, IN_PROGRESS)
        self.assertEqual(blocked.retry_after_seconds, DEFAULT_LEASE_SECONDS)
        self.assertEqual(DEFAULT_LEASE_SECONDS, 30)
        # And no other outcome carries a wait hint.
        self.assertIsNone(store.reserve("sale-lease-4", "hash-a").retry_after_seconds)

    def test_a_claim_for_a_different_request_is_never_taken_over(self) -> None:
        # The reclaim matches the request hash as well as the age, so a slow
        # attempt cannot lose its key to an unrelated submission.
        store = InMemorySaleCommandStore(lease_seconds=EXPIRED_LEASE_SECONDS)
        store.reserve("sale-lease-5", "hash-a")

        time.sleep(EXPIRED_LEASE_WAIT_SECONDS)

        other = store.reserve("sale-lease-5", "hash-b")
        self.assertEqual(other.outcome, IN_PROGRESS)
        self.assertEqual(other.retry_after_seconds, EXPIRED_LEASE_SECONDS)
        # The original holder is untouched, so it can still complete.
        store.complete("sale-lease-5", RECEIPT)
        self.assertEqual(store.reserve("sale-lease-5", "hash-a").outcome, REPLAY)

    def test_a_lease_length_of_no_length_is_refused(self) -> None:
        # There is always a lease, so a length of no length is refused rather
        # than quietly disabling the reclaim.  0 is not an alias for anything:
        # read the naive way it is zero seconds of exclusivity, which is the
        # opposite of holding a key, and read the documented way it hands the
        # same key to two live attempts at once, which is the one thing a
        # reservation exists to prevent.  Holding a claim forever was a
        # recorded defect, so there is nothing to switch back to.
        for lease in (0, -1, -30):
            with self.subTest(lease=lease):
                with self.assertRaises(ValueError):
                    InMemorySaleCommandStore(lease_seconds=lease)

    def test_a_lease_length_that_is_not_a_whole_number_of_seconds_is_refused(self) -> None:
        for lease in (1.5, "30", True, None):
            with self.subTest(lease=lease):
                with self.assertRaises(ValueError):
                    InMemorySaleCommandStore(lease_seconds=cast("int", lease))

    def test_abort_keeps_a_completed_receipt_however_old_it_is(self) -> None:
        store = InMemorySaleCommandStore(lease_seconds=EXPIRED_LEASE_SECONDS)
        store.reserve("sale-lease-8", "hash-a")
        store.complete("sale-lease-8", RECEIPT)
        time.sleep(EXPIRED_LEASE_WAIT_SECONDS)

        store.abort("sale-lease-8")

        replayed = store.reserve("sale-lease-8", "hash-a")
        self.assertEqual(replayed.outcome, REPLAY)
        self.assertEqual(replayed.response, RECEIPT)


@unittest.skipUnless(_database_url(), f"set {DATABASE_ENV} to run")
class PostgresSaleCommandStoreTests(unittest.TestCase):
    """Durability tests against the real temporary PostgreSQL instance."""

    def setUp(self) -> None:
        # A private sale_id per run keeps a shared instance clean and keeps
        # leftovers from an earlier run from failing these tests.
        self.sale_ids = [f"sale-store-{uuid4().hex}" for _ in range(8)]
        self.addCleanup(self._delete_own_rows)

    def _delete_own_rows(self) -> None:
        import psycopg

        with psycopg.connect(_database_url()) as connection:
            connection.execute(
                "DELETE FROM connected_sale_commands WHERE sale_id = ANY(%s)",
                (self.sale_ids,),
            )

    def _expire_claim(self, sale_id: str) -> None:
        """Age a stored claim into the past, the way a crashed attempt would.

        The lease reads the database's own ``now()``, so a test cannot move that
        clock by waiting; it drives the one column the lease compares against,
        through its own connection, instead of sleeping a real lease out.
        """

        import psycopg

        with psycopg.connect(_database_url()) as connection:
            aged = connection.execute(
                "UPDATE connected_sale_commands SET reserved_at = now() - interval '1 hour' "
                "WHERE sale_id = %s",
                (sale_id,),
            )
        self.assertEqual(aged.rowcount, 1, "the claim to expire must exist")

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

    def test_a_pending_row_inside_its_lease_is_refused_with_the_lease_as_its_wait(
        self,
    ) -> None:
        sale_id = self.sale_ids[3]
        first = self.open_store()
        second = self.open_store()

        self.assertEqual(first.reserve(sale_id, "hash-a").outcome, RESERVED)
        held = second.reserve(sale_id, "hash-a")

        # Two connections, so this is a cross-process answer and not a lock in
        # one instance: the second server is told the key is busy and for how
        # long, rather than being left to guess when to come back.
        self.assertEqual(held.outcome, IN_PROGRESS)
        self.assertEqual(held.retry_after_seconds, DEFAULT_LEASE_SECONDS)

    def test_an_expired_pending_row_is_taken_over_by_another_instance(self) -> None:
        sale_id = self.sale_ids[4]
        crashed = self.open_store()
        restarted = self.open_store()

        self.assertEqual(crashed.reserve(sale_id, "hash-a").outcome, RESERVED)
        # The crashed attempt never completed or aborted, which is the whole
        # case the lease exists for.
        self._expire_claim(sale_id)

        reclaimed = restarted.reserve(sale_id, "hash-a")

        # RESERVED, not a replay: no receipt was ever stored, so there is
        # nothing to replay and the sale has still to happen.
        self.assertEqual(reclaimed.outcome, RESERVED)
        self.assertIsNone(reclaimed.response)
        restarted.complete(sale_id, RECEIPT)
        self.assertEqual(restarted.reserve(sale_id, "hash-a").outcome, REPLAY)

    def test_two_instances_racing_one_expired_key_produce_one_reservation(self) -> None:
        sale_id = self.sale_ids[5]
        crashed = self.open_store()
        self.assertEqual(crashed.reserve(sale_id, "hash-a").outcome, RESERVED)
        self._expire_claim(sale_id)

        # Two live stores, so two connections and two transactions, released
        # together so they genuinely contend rather than running in sequence.
        racers = [self.open_store(), self.open_store()]
        start = threading.Barrier(len(racers))
        outcomes: list[str] = []
        lock = threading.Lock()

        def race(store: PostgresSaleCommandStore) -> None:
            start.wait(timeout=10)
            reservation = store.reserve(sale_id, "hash-a")
            with lock:
                outcomes.append(reservation.outcome)

        threads = [threading.Thread(target=race, args=(store,)) for store in racers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        still_running = [thread.name for thread in threads if thread.is_alive()]
        self.assertEqual(still_running, [], "a racer never finished")

        # This is the assertion the whole reclaim rests on.  One statement has
        # to decide the reclaim, and only the racer whose conditional UPDATE
        # matched a row may answer RESERVED; a read-then-write reclaim would
        # let both read an expired row and both write it back, and two live
        # attempts would hold one key and post the sale twice.
        self.assertEqual(len(outcomes), len(racers))
        self.assertEqual(outcomes.count(RESERVED), 1, outcomes)
        self.assertEqual(outcomes.count(IN_PROGRESS), 1, outcomes)

    def test_a_completed_receipt_is_never_taken_over_however_old_it_is(self) -> None:
        sale_id = self.sale_ids[6]
        store = self.open_store()

        store.reserve(sale_id, "hash-a")
        store.complete(sale_id, RECEIPT)
        self._expire_claim(sale_id)

        # The age is the only thing the reclaim looks at, so a stored receipt
        # must be excluded by its status rather than by its age alone.
        replayed = self.open_store().reserve(sale_id, "hash-a")

        self.assertEqual(replayed.outcome, REPLAY)
        self.assertEqual(replayed.response, RECEIPT)

    def test_a_lease_length_of_no_length_is_refused(self) -> None:
        # Refused at construction rather than accepted and misbehaving, in the
        # same way a blank or non-str dsn already is.  The length is validated
        # before the connection is opened, so this needs no table and no row.
        for lease in (0, -1, -30):
            with self.subTest(lease=lease):
                with self.assertRaises(ValueError):
                    PostgresSaleCommandStore(_database_url(), lease_seconds=lease)


if __name__ == "__main__":
    unittest.main()
