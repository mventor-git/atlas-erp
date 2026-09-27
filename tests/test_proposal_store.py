"""Tests for the protocol module's durable proposal store.

The in-memory tests run everywhere.  The PostgreSQL tests run only against the
real temporary instances named by ``ATLAS_ERP_DATABASE_URL`` and
``ATLAS_ERP_CONNECT_DATABASE_URL``, because a fake would not prove that a
decision survives closing and reopening the store -- which is the one thing this
store exists to do, and the one thing the in-memory adapter cannot do at all.
``tests/two_instance_smoke.py`` provisions both and runs this file against them.

The refusal and replay cases are checked *in the owner's database* rather than
in the domain object, because that is where a half-written sale would be: a
durable protocol store must not make a refused command leave a sale behind.
"""

from __future__ import annotations

import json
import os
import re
import unittest
from collections.abc import Mapping
from http.client import HTTPConnection
from typing import Any, cast
from uuid import uuid4

from atlas_erp import (
    ACCEPTED,
    PENDING,
    REJECTED,
    Business,
    BusinessStore,
    ConnectServer,
    InMemoryProposalStore,
    PostgresBusinessStore,
    PostgresProposalStore,
    PostgresSaleCommandStore,
    ProposalStore,
    ProposalStoreError,
    SaleCommandStore,
)
from atlas_erp.business_store import CREATE_SCHEMA_SQL as BUSINESS_SCHEMA_SQL
from atlas_erp.connect_server import (
    CONNECT_DATABASE_ENV,
    _startup_business,
    grant_configured_peers,
)
from atlas_erp.protocol import ProtocolAdapter, ProposalError, ProtocolKernel
from atlas_erp.proposal_store import CREATE_PROPOSALS_SQL
from atlas_erp.sale_store import CREATE_COMMANDS_SQL

DATABASE_ENV = "ATLAS_ERP_DATABASE_URL"
ECOM = "atlas-ecom"
ECOM_TOKEN = "proposal-store-token"
CAPABILITY = "sales.manual_sales"
REASON = "posted sale-by-restart"
PAYLOAD: Mapping[str, object] = {
    "customer_id": "customer-1",
    "sale_id": "sale-1",
    "lines": [{"item_id": "item-1", "quantity": 1, "unit_price_cents": 1250}],
}
# Every table the ERP product declares, read from its own DDL rather than listed
# here, so the check cannot fall behind a table added to either store.
PRODUCT_TABLES = frozenset(
    name
    for statement in (*BUSINESS_SCHEMA_SQL, CREATE_COMMANDS_SQL)
    for name in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", statement)
)
# The one table the protocol store owns, read the same way.
PROTOCOL_TABLES = tuple(
    re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", "\n".join(CREATE_PROPOSALS_SQL))
)


def _database_url() -> str:
    return os.environ.get(DATABASE_ENV, "")


def _connect_database_url() -> str:
    return os.environ.get(CONNECT_DATABASE_ENV, "")


class InMemoryProposalStoreTests(unittest.TestCase):
    """The adapter's default store, and the ceiling it carries.

    These cases pin the semantics both adapters share.  The last one is the
    reason the durable adapter exists and is stated as a test rather than a
    comment: a fresh in-memory store knows nothing about the decision the
    previous one made.
    """

    def setUp(self) -> None:
        self.store = InMemoryProposalStore()
        self.proposal = self.store.create("proposal-1", ECOM, CAPABILITY, PAYLOAD)

    def test_a_created_proposal_is_pending_and_names_its_peer(self) -> None:
        self.assertEqual(self.proposal.state, PENDING)
        self.assertEqual(self.proposal.peer_id, ECOM)
        self.assertEqual(self.proposal.capability, CAPABILITY)
        self.assertEqual(self.proposal.payload, PAYLOAD)
        self.assertIsNone(self.proposal.reason)
        self.assertIsNone(self.proposal.decided_at)
        self.assertTrue(self.proposal.created_at, "a proposal records when it was made")

    def test_a_decision_is_terminal_and_carries_its_reason_and_time(self) -> None:
        decided = self.store.transition("proposal-1", ACCEPTED, REASON)

        self.assertEqual(decided.state, ACCEPTED)
        self.assertEqual(decided.reason, REASON)
        self.assertIsNotNone(decided.decided_at)
        for state in (ACCEPTED, REJECTED):
            with self.subTest(state=state), self.assertRaises(ProposalStoreError):
                self.store.transition("proposal-1", state, "a second decision")

    def test_a_proposal_can_only_be_accepted_or_rejected(self) -> None:
        with self.assertRaises(ProposalStoreError):
            self.store.transition("proposal-1", PENDING, "not a decision")

    def test_a_missing_or_duplicated_proposal_is_refused(self) -> None:
        with self.assertRaises(ProposalStoreError):
            self.store.get("proposal-missing")
        with self.assertRaises(ProposalStoreError):
            self.store.create("proposal-1", ECOM, CAPABILITY, PAYLOAD)

    def test_a_stored_payload_is_not_the_callers_dict(self) -> None:
        payload = cast("dict[str, object]", dict(PAYLOAD))
        self.store.create("proposal-2", ECOM, CAPABILITY, payload)
        stored = cast("Mapping[str, object]", self.store.get("proposal-2").payload)

        payload["sale_id"] = "rewritten-after-the-fact"

        self.assertEqual(stored["sale_id"], "sale-1")

    def test_all_returns_every_record_keyed_by_its_id(self) -> None:
        self.store.create("proposal-2", ECOM, CAPABILITY, PAYLOAD)

        self.assertEqual(
            sorted(self.store.all()), ["proposal-1", "proposal-2"]
        )

    def test_a_fresh_in_memory_store_has_forgotten_the_decision(self) -> None:
        # The ceiling, asserted.  Nothing here is broken: this is what a
        # process-local decision looks like from the next process, and it is the
        # reason ATLAS_ERP_CONNECT_DATABASE_URL exists.
        decided = self.store.transition("proposal-1", ACCEPTED, REASON)

        after_restart = InMemoryProposalStore()

        with self.assertRaises(ProposalStoreError):
            after_restart.get(decided.proposal_id)


class _KernelStoreTests(unittest.TestCase):
    """The module's own path to a proposal, over whatever store it was given."""

    def kernel(self, store: ProposalStore | None = None) -> ProtocolKernel:
        protocol = ProtocolKernel(Business().registry, ProtocolAdapter(store))
        protocol.grant(CAPABILITY, ECOM, "proposer")
        return protocol

    def test_the_kernel_records_the_peer_and_answers_from_the_store(self) -> None:
        store = InMemoryProposalStore()
        protocol = self.kernel(store)

        proposal = protocol.submit_proposal(CAPABILITY, PAYLOAD, peer_id=ECOM)
        decided = protocol.resolve_proposal(
            proposal.proposal_id, ACCEPTED, REASON, peer_id=None
        )

        self.assertEqual(decided.peer_id, ECOM)
        self.assertEqual(decided.reason, REASON)
        # The record is the store's, not the adapter's private state.
        self.assertEqual(store.all()[proposal.proposal_id], decided)
        self.assertEqual(protocol.adapter.proposals, store.all())

    def test_the_kernel_reports_a_missing_proposal_as_a_protocol_error(self) -> None:
        protocol = self.kernel()

        with self.assertRaises(ProposalError):
            protocol.resolve_proposal("proposal-missing", ACCEPTED, REASON)


@unittest.skipUnless(
    _connect_database_url(), f"set {CONNECT_DATABASE_ENV} to run"
)
class PostgresProposalStoreTests(unittest.TestCase):
    """The decision outlives the store that made it."""

    def setUp(self) -> None:
        self.prefix = f"proposal-{uuid4().hex[:12]}"
        self.addCleanup(self._delete_own_rows)

    def open_store(self) -> PostgresProposalStore:
        store = PostgresProposalStore(_connect_database_url())
        self.addCleanup(store.close)
        return store

    def _delete_own_rows(self) -> None:
        import psycopg

        with psycopg.connect(_connect_database_url()) as connection:
            connection.execute(
                "DELETE FROM connect_proposals WHERE proposal_id LIKE %s",
                (f"{self.prefix}%",),
            )

    def test_a_pending_proposal_survives_a_close_and_a_reopen(self) -> None:
        # The point of the module, as one test: a proposal made before a restart
        # is still there afterwards, byte for byte, and can still be decided by
        # the process that comes next.  Swapping the store below for the
        # in-memory one fails this case, which is what makes it a test of
        # durability rather than of the store's in-process behaviour.
        store = self.open_store()
        proposal = store.create(
            f"{self.prefix}-1", ECOM, CAPABILITY, PAYLOAD
        )
        store.close()

        reopened = self.open_store()
        found = reopened.get(proposal.proposal_id)

        self.assertEqual(found, proposal)
        self.assertEqual(found.state, PENDING)
        self.assertIsNone(found.decided_at)

    def test_a_decision_made_after_a_reopen_is_durable_and_terminal(self) -> None:
        store = self.open_store()
        proposal = store.create(f"{self.prefix}-1", ECOM, CAPABILITY, PAYLOAD)
        store.close()

        decided = self.open_store().transition(proposal.proposal_id, ACCEPTED, REASON)

        self.assertEqual(decided.state, ACCEPTED)
        self.assertEqual(decided.reason, REASON)
        self.assertIsNotNone(decided.decided_at)
        # A third store is a second restart, and it reads the decision rather
        # than the pending request that preceded it.
        after_restart = self.open_store().get(proposal.proposal_id)
        self.assertEqual(after_restart, decided)
        with self.assertRaises(ProposalStoreError):
            self.open_store().transition(proposal.proposal_id, REJECTED, "too late")

    def test_a_second_store_cannot_decide_the_same_proposal_twice(self) -> None:
        # Two stores on one database are two servers, and the row count is what
        # decides the winner: the loser is told the proposal is already decided
        # rather than overwriting the first decision.
        first = self.open_store()
        second = self.open_store()
        proposal = first.create(f"{self.prefix}-1", ECOM, CAPABILITY, PAYLOAD)

        decided = first.transition(proposal.proposal_id, ACCEPTED, REASON)

        with self.assertRaises(ProposalStoreError) as refused:
            second.transition(proposal.proposal_id, REJECTED, "a second decision")
        self.assertIn("already accepted", str(refused.exception))
        self.assertEqual(
            second.get(proposal.proposal_id), decided, "the first decision stands"
        )

    def test_a_duplicate_id_is_refused_rather_than_overwriting(self) -> None:
        store = self.open_store()
        original = store.create(f"{self.prefix}-1", ECOM, CAPABILITY, PAYLOAD)

        with self.assertRaises(ProposalStoreError):
            store.create(f"{self.prefix}-1", ECOM, CAPABILITY, {"sale_id": "other"})

        self.assertEqual(store.get(original.proposal_id).payload, PAYLOAD)

    def test_the_protocol_store_holds_no_product_table(self) -> None:
        # Read from the live catalog rather than from configuration: the claim
        # is that this database holds the protocol's record and nothing a
        # product owns.
        import psycopg

        with psycopg.connect(_connect_database_url(), connect_timeout=5) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            }

        self.assertEqual(sorted(PROTOCOL_TABLES), ["connect_proposals"])
        self.assertEqual(
            sorted(tables & PRODUCT_TABLES),
            [],
            f"the protocol store holds product tables {sorted(tables & PRODUCT_TABLES)}",
        )

    def test_a_row_policy_confines_every_role_to_its_own_peer(self) -> None:
        # The policy is in the catalog rather than in a code path a caller could
        # forget, and it names no role and holds no credential.  PostgreSQL
        # stores the expression it parsed, so the comparison is case-insensitive:
        # the catalog holds ``CURRENT_USER``, not the spelling in the DDL.
        import psycopg

        with psycopg.connect(_connect_database_url(), connect_timeout=5) as connection:
            enabled = connection.execute(
                "SELECT relrowsecurity FROM pg_class WHERE relname = 'connect_proposals'"
            ).fetchone()
            policies = [
                (str(row[0]).lower(), str(row[1]).lower(), str(row[2]).lower(), str(row[3]).lower())
                for row in connection.execute(
                    "SELECT policyname, cmd, qual, with_check FROM pg_policies "
                    "WHERE tablename = 'connect_proposals'"
                )
            ]

        self.assertEqual(enabled, (True,))
        self.assertEqual(len(policies), 1, policies)
        self.assertEqual(policies[0][1], "all")
        self.assertIn("peer_id", policies[0][2])
        self.assertIn("current_user", policies[0][2])
        self.assertIn("current_user", policies[0][3])


@unittest.skipUnless(
    _database_url() and _connect_database_url(),
    f"set {DATABASE_ENV} and {CONNECT_DATABASE_ENV} to run",
)
class DurableProposalOverTheWireTests(unittest.TestCase):
    """The connected sale, driven through a restart with a durable decision.

    Every case here builds its server the way ``main`` does -- from
    :func:`~atlas_erp.connect_server._startup_business` with both durable stores
    -- so what is exercised is the startup path, not a hand-built arrangement.
    """

    def setUp(self) -> None:
        self.suffix = uuid4().hex[:12]
        self.sale_id = f"sale-durable-{self.suffix}"
        self.business_store = PostgresBusinessStore(_database_url())
        self.addCleanup(self.business_store.close)
        self.sale_store = PostgresSaleCommandStore(_database_url())
        self.addCleanup(self.sale_store.close)
        self.proposal_store = self.open_proposal_store()
        self.addCleanup(self._delete_own_rows)
        self.business, self.protocol = _startup_business(
            self.business_store, self.proposal_store
        )
        grant_configured_peers(self.protocol)
        # After the domain exists, and before the server takes a request: the
        # stock this case sells has to be on hand before the sale is refused for
        # want of it.
        self.item_id = self.own_item(quantity=6)
        self.server = self.serve(
            self.business, self.protocol, self.business_store, self.sale_store
        )

    def own_item(self, quantity: int) -> str:
        """Register one item and receive stock for it, written through to the owner.

        Each case owns its inventory rather than sharing the fixture's, because
        the store these tests run against is a real one that earlier cases and the
        smoke itself have already sold from: a case that borrowed the fixture's
        stock would be refused for exhaustion rather than for the reason it names.
        """
        item_id = f"item-{self.suffix}"
        item = self.business.register_item(item_id, f"SKU-{self.suffix}", "Widget", 1250)
        order = self.business.create_purchase_order(
            "supplier-1", [(item_id, quantity)], order_id=f"po-{self.suffix}"
        )
        receipt = self.business.receive_purchase_order(
            order.order_id, receipt_id=f"receipt-{self.suffix}"
        )
        with self.business_store.transaction():
            self.business_store.save_item(item)
            self.business_store.save_purchase_order(order)
            self.business_store.save_receipt(receipt)
            self.business_store.save_movements(self._new_movements)
        return item_id

    @property
    def _new_movements(self) -> list[Any]:
        """The movements this case's receipt added, and nobody else's.

        Saved rather than recomputed so the durable movements are the ones the
        domain itself minted, which is the rule the transport follows too.
        """

        return [
            movement
            for movement in self.business.stock_movements.values()
            if f"{self.suffix}" in movement.movement_id
        ]

    def open_proposal_store(self) -> ProposalStore:
        store: ProposalStore = PostgresProposalStore(_connect_database_url())
        self.addCleanup(store.close)
        return store

    def _delete_own_rows(self) -> None:
        import psycopg

        own = f"%{self.suffix}%"
        with psycopg.connect(_connect_database_url()) as connection:
            connection.execute(
                "DELETE FROM connect_proposals WHERE peer_id = %s", (ECOM,)
            )
        with psycopg.connect(_database_url()) as connection:
            for statement in (
                "DELETE FROM sale_lines WHERE sale_id LIKE %s",
                "DELETE FROM journal_lines WHERE journal_id LIKE %s",
                "DELETE FROM journal_entries WHERE journal_id LIKE %s",
                "DELETE FROM sales WHERE sale_id LIKE %s",
                "DELETE FROM stock_movements WHERE movement_id LIKE %s",
                "DELETE FROM connected_sale_commands WHERE sale_id LIKE %s",
                "DELETE FROM receipt_lines WHERE receipt_id LIKE %s",
                "DELETE FROM receipts WHERE receipt_id LIKE %s",
                "DELETE FROM purchase_order_lines WHERE order_id LIKE %s",
                "DELETE FROM purchase_orders WHERE order_id LIKE %s",
                "DELETE FROM master_items WHERE item_id LIKE %s",
            ):
                connection.execute(statement, (own,))

    def serve(
        self,
        business: Business,
        protocol: ProtocolKernel,
        business_store: BusinessStore,
        sale_store: SaleCommandStore,
    ) -> ConnectServer:
        server = ConnectServer(
            business,
            protocol,
            {ECOM: ECOM_TOKEN},
            port=0,
            sale_store=sale_store,
            business_store=business_store,
        )
        server.start()
        self.addCleanup(server.close)
        return server

    def post_sale(self, server: ConnectServer, sale_id: str, quantity: int = 1) -> Any:
        connection = HTTPConnection(*server.address, timeout=5)
        body = json.dumps(
            {
                "customer_id": "customer-1",
                "sale_id": sale_id,
                "lines": [
                    {
                        "item_id": self.item_id,
                        "quantity": quantity,
                        "unit_price_cents": 1250,
                    }
                ],
            }
        ).encode("utf-8")
        try:
            connection.request(
                "POST",
                "/connect/sales",
                body=body,
                headers={
                    "Authorization": f"Bearer {ECOM_TOKEN}",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def owner_database(self, statement: str, *parameters: object) -> Any:
        import psycopg

        query: Any = statement
        with psycopg.connect(_database_url(), connect_timeout=5) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return rows[0][0] if rows else None

    def owner_sales(self, sale_id: str) -> int:
        """How many sales the owner's database holds for one id.

        Counted by id rather than in total: the store these tests run against may
        already hold the smoke's own sales, and a total would make the assertion
        about the fixture instead of about this test.
        """

        return int(
            self.owner_database("SELECT count(*) FROM sales WHERE sale_id = %s", sale_id)
        )

    def proposals_for(self, sale_id: str) -> list[Any]:
        return [
            proposal
            for proposal in self.proposal_store.all().values()
            if isinstance(proposal.payload, Mapping)
            and proposal.payload.get("sale_id") == sale_id
        ]

    def test_the_decision_survives_a_restart_and_a_replay_decides_nothing_twice(
        self,
    ) -> None:
        status, first = self.post_sale(self.server, self.sale_id)
        self.assertEqual(status, 201, first)
        decided = self.proposals_for(self.sale_id)
        self.assertEqual(len(decided), 1)
        self.assertEqual(decided[0].state, ACCEPTED)
        self.assertEqual(decided[0].reason, f"posted {self.sale_id}")
        self.assertIsNotNone(decided[0].decided_at)

        # The restart: the server is closed, and closing it closes the stores it
        # was given, so every store below is a new one -- a second process's
        # worth of state built from the databases alone.
        self.server.close()
        restarted_business_store = PostgresBusinessStore(_database_url())
        self.addCleanup(restarted_business_store.close)
        restarted_sale_store = PostgresSaleCommandStore(_database_url())
        self.addCleanup(restarted_sale_store.close)
        self.proposal_store = self.open_proposal_store()
        business, protocol = _startup_business(
            restarted_business_store, self.proposal_store
        )
        grant_configured_peers(protocol)
        server = self.serve(
            business, protocol, restarted_business_store, restarted_sale_store
        )

        # The decision is still there, unchanged, with the time it was made.
        survived = self.proposals_for(self.sale_id)
        self.assertEqual(survived, decided, "the decision did not survive the restart")
        # And the restored domain still holds the sale, so the restart restored
        # the effect rather than replaying it.
        self.assertIn(self.sale_id, list(business.sales))
        self.assertEqual(self.owner_sales(self.sale_id), 1, "the sale was applied twice")

        replay_status, replay = self.post_sale(server, self.sale_id)

        self.assertEqual(replay_status, 201)
        self.assertEqual(replay, first, "a replay answered from the stored receipt")
        self.assertEqual(
            len(self.proposals_for(self.sale_id)),
            1,
            "the master decided twice for one idempotency key",
        )
        self.assertEqual(
            self.owner_sales(self.sale_id),
            1,
            "the replay applied the sale a second time",
        )
        self.assertEqual(
            self.owner_database(
                "SELECT count(*) FROM journal_entries WHERE sale_id = %s", self.sale_id
            ),
            1,
            "the replay posted a second journal",
        )
        # One journal entry with two lines, not two entries: a journal is counted
        # by entry because that is the record, and its balance is the property
        # worth keeping.
        self.assertIs(
            self.owner_database(
                "SELECT sum(debit_cents) = sum(credit_cents) FROM journal_lines"
                " WHERE journal_id = %s",
                f"journal-{self.sale_id}",
            ),
            True,
            "the replay left the sale's journal unbalanced",
        )

    def test_a_refused_proposal_leaves_no_sale_no_journal_and_no_movement(self) -> None:
        # The "nothing half-written" property, measured in the owner's own
        # database this time: a decision that is now durable must not make a
        # refusal leave anything behind to be found after a restart.
        refused_sale_id = f"{self.sale_id}-too-many"

        # One more than the stock this case owns, so the domain's own invariant
        # is what refuses it.
        status, payload = self.post_sale(self.server, refused_sale_id, quantity=7)

        self.assertEqual((status, payload), (409, {"error": "insufficient stock"}))
        self.assertEqual(
            self.owner_sales(refused_sale_id),
            0,
            "a refused proposal left a sale in the owner's database",
        )
        self.assertEqual(
            self.owner_database(
                "SELECT count(*) FROM journal_entries WHERE sale_id = %s",
                refused_sale_id,
            ),
            0,
            "a refused proposal posted a journal in the owner's database",
        )
        self.assertEqual(
            self.owner_database(
                "SELECT count(*) FROM stock_movements WHERE reference_id = %s",
                refused_sale_id,
            ),
            0,
            "a refused proposal moved stock",
        )
        # And the refusal is on the durable record with the domain's own reason.
        refused = self.proposals_for(refused_sale_id)
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0].state, REJECTED)
        self.assertEqual(refused[0].reason, "insufficient stock")
        # The key was released, so the same sale id works once it is affordable.
        self.assertEqual(
            self.owner_database(
                "SELECT count(*) FROM connected_sale_commands WHERE sale_id = %s",
                refused_sale_id,
            ),
            0,
            "a refused command burned its idempotency key",
        )
        self.assertEqual(self.post_sale(self.server, refused_sale_id)[0], 201)
        # Both decisions are on the durable record: the refusal, with the
        # domain's own reason, and the retry's acceptance.  Two proposals and one
        # sale, because the refusal released the key rather than burning it.
        self.assertEqual(
            sorted(
                (proposal.state, str(proposal.reason))
                for proposal in self.proposals_for(refused_sale_id)
            ),
            [
                ("accepted", f"posted {refused_sale_id}"),
                ("rejected", "insufficient stock"),
            ],
        )
        self.assertEqual(self.owner_sales(refused_sale_id), 1)

    def test_a_replayed_key_returns_the_stored_receipt_and_creates_no_proposal(
        self,
    ) -> None:
        first_status, first = self.post_sale(self.server, self.sale_id)
        second_status, second = self.post_sale(self.server, self.sale_id)

        self.assertEqual((first_status, second_status), (201, 201))
        self.assertEqual(first, second)
        self.assertEqual(len(self.proposals_for(self.sale_id)), 1)
        self.assertEqual(self.owner_sales(self.sale_id), 1, "the replay sold twice")
        self.assertEqual(
            self.owner_database(
                "SELECT count(*) FROM connected_sale_commands"
                " WHERE sale_id = %s AND status = 'completed'",
                self.sale_id,
            ),
            1,
            "the replay completed a second receipt",
        )
